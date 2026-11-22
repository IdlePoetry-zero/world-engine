[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$Stop,
    [string]$ExecutablePath = 'E:\AI-demo\AI小说转漫画\.runtime\ollama\ollama.exe',
    [string]$ModelsDirectory = 'E:\AI-demo\AI小说转漫画\.models\ollama',
    [ValidateRange(1, 65535)]
    [int]$Port = 11434
)

$ErrorActionPreference = 'Stop'
$script:FailureCode = 'operation_failed'
$script:ArchivedOwnerRecordPath = $null

# Upstream documents OLLAMA_HOST, OLLAMA_MODELS, OLLAMA_NO_CLOUD,
# OLLAMA_NOPRUNE and OLLAMA_NUM_PARALLEL in envconfig/config.go.
# Only these reviewed settings are applied; no model pull is initiated here.
# https://github.com/ollama/ollama/blob/main/envconfig/config.go

function Resolve-ExistingPath {
    param(
        [Parameter(Mandatory)][string]$PathValue,
        [Parameter(Mandatory)][ValidateSet('File', 'Directory')][string]$Kind,
        [Parameter(Mandatory)][string]$FailureCode
    )

    $script:FailureCode = $FailureCode
    if (-not [System.IO.Path]::IsPathRooted($PathValue)) {
        throw 'Expected an absolute path.'
    }

    $resolved = Resolve-Path -LiteralPath $PathValue -ErrorAction Stop
    $item = Get-Item -LiteralPath $resolved.Path -ErrorAction Stop
    if ($Kind -eq 'File' -and $item.PSIsContainer) {
        $script:FailureCode = $FailureCode
        throw 'Expected an existing file.'
    }
    if ($Kind -eq 'Directory' -and -not $item.PSIsContainer) {
        $script:FailureCode = $FailureCode
        throw 'Expected an existing directory.'
    }

    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        $item = $item.ResolveLinkTarget($true)
        if ($null -eq $item) {
            $script:FailureCode = $FailureCode
            throw 'Could not resolve the path target.'
        }
    }

    return [System.IO.Path]::GetFullPath($item.FullName)
}

function Get-PortOwnership {
    param([Parameter(Mandatory)][int]$TargetPort)

    try {
        # The port-filtered CIM query raises ObjectNotFound when the port is free.
        # Filter a successful inventory so an empty result still permits startup.
        $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq $TargetPort)
    }
    catch {
        $script:FailureCode = 'port_ownership_unavailable'
        throw 'Could not verify local port ownership.'
    }

    $owners = @($listeners | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
    $addresses = @($listeners | ForEach-Object { [string]$_.LocalAddress } | Sort-Object -Unique)
    return @{
        listener_count = $listeners.Count
        owner_pids = $owners
        local_addresses = $addresses
    }
}

function Get-ResolvedProcessPath {
    param([Parameter(Mandatory)][int]$ProcessId)

    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        if ([string]::IsNullOrWhiteSpace($process.Path)) {
            return $null
        }
        return Resolve-ExistingPath -PathValue $process.Path -Kind File -FailureCode 'process_path_unavailable'
    }
    catch {
        return $null
    }
}

function Get-ProcessStartTimeUtcTicks {
    param([Parameter(Mandatory)][int]$ProcessId)

    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        return [long]$process.StartTime.ToUniversalTime().Ticks
    }
    catch {
        return $null
    }
}

function Read-OwnerRecord {
    param([Parameter(Mandatory)][string]$PathValue)

    if (-not (Test-Path -LiteralPath $PathValue -PathType Leaf)) {
        return @{ present = $false; valid = $false; record = $null }
    }

    try {
        $originalBytes = [System.IO.File]::ReadAllBytes($PathValue)
        $contents = [System.Text.UTF8Encoding]::new($false, $true).GetString($originalBytes)
        if ($contents.Length -gt 0 -and $contents[0] -eq [char]0xFEFF) {
            $contents = $contents.Substring(1)
        }
        $record = $contents | ConvertFrom-Json -ErrorAction Stop
        return @{ present = $true; valid = $true; record = $record; original_bytes = $originalBytes }
    }
    catch {
        return @{ present = $true; valid = $false; record = $null; original_bytes = $null }
    }
}

function Test-JsonInteger {
    param([Parameter(Mandatory)][AllowNull()]$Value)

    if ($null -eq $Value) { return $false }
    return ($Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or
        $Value -is [uint16] -or $Value -is [int] -or $Value -is [uint32] -or
        $Value -is [long] -or $Value -is [uint64])
}

function Test-ExactSchema2OwnerRecord {
    param(
        [Parameter(Mandatory)][AllowNull()]$Record,
        [Parameter(Mandatory)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory)][string]$ProjectRoot,
        [Parameter(Mandatory)][int]$ExpectedPort
    )

    if ($null -eq $Record -or $Record -is [System.Array]) { return $false }
    $requiredFields = @(
        'schema_version', 'manager', 'project_root', 'pid', 'start_time_utc_ticks',
        'executable_path', 'port', 'started_at', 'stdout_log', 'stderr_log'
    )
    foreach ($field in $requiredFields) {
        if ($null -eq $Record.PSObject.Properties[$field]) { return $false }
    }

    if (-not (Test-JsonInteger -Value $Record.schema_version) -or
        -not (Test-JsonInteger -Value $Record.pid) -or
        -not (Test-JsonInteger -Value $Record.start_time_utc_ticks) -or
        -not (Test-JsonInteger -Value $Record.port)) {
        return $false
    }
    foreach ($field in @('manager', 'project_root', 'executable_path', 'stdout_log', 'stderr_log')) {
        if ($Record.$field -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$Record.$field)) {
            return $false
        }
    }
    if ($Record.started_at -is [string]) {
        $parsedStartedAt = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse(
            [string]$Record.started_at,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None,
            [ref]$parsedStartedAt
        )) { return $false }
    }
    elseif ($Record.started_at -is [DateTime]) {
        if ($Record.started_at -eq [DateTime]::MinValue) { return $false }
    }
    elseif ($Record.started_at -is [DateTimeOffset]) {
        if ($Record.started_at -eq [DateTimeOffset]::MinValue) { return $false }
    }
    else {
        return $false
    }
    if (-not [System.IO.Path]::IsPathRooted([string]$Record.stdout_log) -or
        -not [System.IO.Path]::IsPathRooted([string]$Record.stderr_log)) { return $false }

    try {
        $schemaVersion = [long]$Record.schema_version
        $recordPid = [long]$Record.pid
        $startTicks = [long]$Record.start_time_utc_ticks
        $recordPort = [long]$Record.port
    }
    catch {
        return $false
    }
    if ($recordPid -gt [int]::MaxValue) { return $false }

    return [bool](
        $schemaVersion -eq 2 -and
        [string]$Record.manager -ceq 'world-engine-local-ollama' -and
        [string]$Record.project_root -ieq $ProjectRoot -and
        [string]$Record.executable_path -ieq $ExpectedExecutablePath -and
        $recordPort -eq $ExpectedPort -and
        $recordPid -gt 0 -and
        $startTicks -gt 0
    )
}

function Get-RecordedProcessPresence {
    param([Parameter(Mandatory)][int]$ProcessId)

    try {
        $null = Get-Process -Id $ProcessId -ErrorAction Stop
        return 'present'
    }
    catch {
        if ([string]$_.FullyQualifiedErrorId -like 'NoProcessFoundForGivenId,*') {
            return 'absent'
        }
        return 'unknown'
    }
}

function Test-ByteArraysEqual {
    param(
        [Parameter(Mandatory)][byte[]]$Left,
        [Parameter(Mandatory)][byte[]]$Right
    )

    if ($Left.Length -ne $Right.Length) { return $false }
    for ($index = 0; $index -lt $Left.Length; $index++) {
        if ($Left[$index] -ne $Right[$index]) { return $false }
    }
    return $true
}

function Archive-StaleOwnerRecordIfSafe {
    param(
        [Parameter(Mandatory)][string]$OwnerPath,
        [Parameter(Mandatory)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory)][string]$ProjectRoot,
        [Parameter(Mandatory)][int]$ExpectedPort
    )

    $ownerState = Read-OwnerRecord -PathValue $OwnerPath
    if (-not $ownerState.present -or -not $ownerState.valid -or
        -not (Test-ExactSchema2OwnerRecord -Record $ownerState.record -ExpectedExecutablePath $ExpectedExecutablePath -ProjectRoot $ProjectRoot -ExpectedPort $ExpectedPort)) {
        return $null
    }

    $processPresence = Get-RecordedProcessPresence -ProcessId ([int]$ownerState.record.pid)
    if ($processPresence -ne 'absent') { return $null }

    $portState = Get-PortOwnership -TargetPort $ExpectedPort
    if ($portState.listener_count -ne 0) { return $null }

    $currentBytes = [System.IO.File]::ReadAllBytes($OwnerPath)
    if (-not (Test-ByteArraysEqual -Left $ownerState.original_bytes -Right $currentBytes)) {
        return $null
    }

    $archiveDirectory = [System.IO.Path]::GetDirectoryName($OwnerPath)
    do {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $nonce = [Guid]::NewGuid().ToString('N')
        $archivePath = Join-Path $archiveDirectory "owner.stale-$stamp-$nonce.json"
    } while (Test-Path -LiteralPath $archivePath)

    try {
        # File.Move preserves the exact old bytes and refuses to replace an existing archive.
        [System.IO.File]::Move($OwnerPath, $archivePath)
        $archivedBytes = [System.IO.File]::ReadAllBytes($archivePath)
        if (-not (Test-ByteArraysEqual -Left $ownerState.original_bytes -Right $archivedBytes)) {
            $script:FailureCode = 'ownership_record_archive_mismatch'
            throw 'The archived ownership record differs from the record that was verified.'
        }
        return $archivePath
    }
    catch {
        if ($script:FailureCode -ne 'ownership_record_archive_mismatch') {
            $script:FailureCode = 'ownership_record_archive_failed'
        }
        throw 'Could not archive the verified stale ownership record; no process was started.'
    }
}

function Test-ManagedOwnership {
    param(
        [Parameter(Mandatory)][string]$OwnerPath,
        [Parameter(Mandatory)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory)][string]$ProjectRoot,
        [Parameter(Mandatory)][int]$ExpectedPort
    )

    $ownerState = Read-OwnerRecord -PathValue $OwnerPath
    if (-not $ownerState.present) {
        return @{ record_present = $false; record_valid = $false; owned = $false; record = $null; port = $null }
    }
    if (-not $ownerState.valid) {
        return @{ record_present = $true; record_valid = $false; owned = $false; record = $null; port = $null }
    }

    $record = $ownerState.record
    try {
        $recordPid = [int]$record.pid
        $recordPort = [int]$record.port
        $recordStartTicks = [long]$record.start_time_utc_ticks
        $recordPath = [string]$record.executable_path
        $recordRoot = [string]$record.project_root
    }
    catch {
        return @{ record_present = $true; record_valid = $false; owned = $false; record = $record; port = $null }
    }

    $configurationMatches = (
        # Version 2 requires the process creation time so a reused PID can
        # never inherit ownership from an older managed process.
        $record.schema_version -eq 2 -and
        $record.manager -eq 'world-engine-local-ollama' -and
        $recordRoot -ieq $ProjectRoot -and
        $recordPath -ieq $ExpectedExecutablePath -and
        $recordPort -eq $ExpectedPort -and
        $recordPid -gt 0 -and
        $recordStartTicks -gt 0
    )
    if (-not $configurationMatches) {
        return @{ record_present = $true; record_valid = $true; owned = $false; record = $record; port = $recordPort }
    }

    $actualPath = Get-ResolvedProcessPath -ProcessId $recordPid
    $pathMatches = $null -ne $actualPath -and $actualPath -ieq $ExpectedExecutablePath
    $actualStartTicks = Get-ProcessStartTimeUtcTicks -ProcessId $recordPid
    $startTimeMatches = $null -ne $actualStartTicks -and [long]$actualStartTicks -eq $recordStartTicks
    $portState = Get-PortOwnership -TargetPort $recordPort
    $portMatches = (
        $portState.listener_count -gt 0 -and
        $portState.owner_pids.Count -eq 1 -and
        [int]$portState.owner_pids[0] -eq $recordPid
    )

    return @{
        record_present = $true
        record_valid = $true
        owned = [bool]($configurationMatches -and $pathMatches -and $startTimeMatches -and $portMatches)
        record = $record
        port = $recordPort
        process_path_matches = [bool]$pathMatches
        start_time_matches = [bool]$startTimeMatches
        port_matches = [bool]$portMatches
        port_owner_pids = $portState.owner_pids
    }
}

function Get-LocalJson {
    param(
        [Parameter(Mandatory)][System.Net.Http.HttpClient]$Client,
        [Parameter(Mandatory)][string]$Uri
    )

    try {
        $response = $Client.GetAsync($Uri).GetAwaiter().GetResult()
        try {
            $statusCode = [int]$response.StatusCode
            if (-not $response.IsSuccessStatusCode) {
                return @{ ok = $false; status_code = $statusCode; value = $null }
            }
            $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            return @{ ok = $true; status_code = $statusCode; value = ($content | ConvertFrom-Json -ErrorAction Stop) }
        }
        finally {
            $response.Dispose()
        }
    }
    catch {
        return @{ ok = $false; status_code = $null; value = $null }
    }
}

function Get-OllamaApiStatus {
    param(
        [Parameter(Mandatory)][int]$TargetPort,
        [ValidateRange(0.1, 3.0)][double]$TimeoutSeconds = 2.0
    )

    if ($null -eq ('System.Net.Http.HttpClientHandler' -as [type])) {
        Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    }
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    try {
        $baseUri = "http://127.0.0.1:$TargetPort"
        $versionResult = Get-LocalJson -Client $client -Uri "$baseUri/api/version"
        $tagsResult = Get-LocalJson -Client $client -Uri "$baseUri/api/tags"
        $version = if ($versionResult.ok -and $versionResult.value.version) { [string]$versionResult.value.version } else { $null }
        $models = @()
        if ($tagsResult.ok -and $null -ne $tagsResult.value.models) {
            $models = @($tagsResult.value.models | ForEach-Object {
                if (-not [string]::IsNullOrWhiteSpace([string]$_.name)) { [string]$_.name }
            })
        }
        return @{
            ready = [bool]($versionResult.ok -and $tagsResult.ok -and $null -ne $version)
            version = $version
            models = $models
            version_http_status = $versionResult.status_code
            tags_http_status = $tagsResult.status_code
        }
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Write-Result {
    param([Parameter(Mandatory)][hashtable]$Result)
    Write-Output ($Result | ConvertTo-Json -Depth 8 -Compress)
}

function Invoke-Status {
    param(
        [string]$ExpectedExecutablePath,
        [string]$ProjectRoot,
        [string]$OwnerPath,
        [int]$TargetPort
    )

    $ownership = Test-ManagedOwnership -OwnerPath $OwnerPath -ExpectedExecutablePath $ExpectedExecutablePath -ProjectRoot $ProjectRoot -ExpectedPort $TargetPort
    $portState = Get-PortOwnership -TargetPort $TargetPort
    $api = Get-OllamaApiStatus -TargetPort $TargetPort -TimeoutSeconds 2
    $recordPid = if ($ownership.record_valid -and $ownership.record) { [int]$ownership.record.pid } else { $null }
    $state = if ($ownership.owned) { 'owned_verified' } elseif ($ownership.record_present) { 'ownership_record_unverified' } else { 'not_owned' }

    Write-Result @{
        ok = $true
        mode = 'status'
        base_url = "http://127.0.0.1:$TargetPort"
        owned = [bool]$ownership.owned
        reused = [bool]($api.ready -and -not $ownership.owned)
        ownership_state = $state
        owner_record_present = [bool]$ownership.record_present
        recorded_pid = $recordPid
        port_owner_pids = $portState.owner_pids
        listener_addresses = $portState.local_addresses
        service_ready = [bool]$api.ready
        version = $api.version
        models = @($api.models)
        version_http_status = $api.version_http_status
        tags_http_status = $api.tags_http_status
        note = if ($api.ready -and -not $ownership.owned) { 'Healthy local service is reused; this script does not claim ownership of it.' } else { $null }
    }
}

function Invoke-Start {
    param(
        [string]$ExpectedExecutablePath,
        [string]$ExpectedModelsPath,
        [string]$ProjectRoot,
        [string]$ManagedDirectory,
        [string]$OwnerPath,
        [string]$LogDirectory,
        [int]$TargetPort
    )

    $ownership = Test-ManagedOwnership -OwnerPath $OwnerPath -ExpectedExecutablePath $ExpectedExecutablePath -ProjectRoot $ProjectRoot -ExpectedPort $TargetPort
    $portState = Get-PortOwnership -TargetPort $TargetPort
    $initialApi = Get-OllamaApiStatus -TargetPort $TargetPort -TimeoutSeconds 1

    if ($ownership.record_present) {
        if ($ownership.owned) {
            Write-Result @{
                ok = [bool]$initialApi.ready
                mode = 'start'
                started = $false
                owned = $true
                reused = $false
                ownership_state = 'owned_verified'
                pid = [int]$ownership.record.pid
                service_ready = [bool]$initialApi.ready
                version = $initialApi.version
                models = @($initialApi.models)
                note = if ($initialApi.ready) { 'Existing project-owned service is already ready.' } else { 'The verified project process exists but its local API is not ready; it was not restarted.' }
            }
            return
        }
        if ($initialApi.ready) {
            Write-Result @{
                ok = $true
                mode = 'start'
                started = $false
                owned = $false
                reused = $true
                ownership_state = 'ownership_record_unverified'
                port_owner_pids = $portState.owner_pids
                service_ready = $true
                version = $initialApi.version
                models = @($initialApi.models)
                note = 'A healthy local service is available, but its ownership record does not match; it was reused without changing ownership.'
            }
            return
        }

        $archivedOwnerPath = Archive-StaleOwnerRecordIfSafe -OwnerPath $OwnerPath -ExpectedExecutablePath $ExpectedExecutablePath -ProjectRoot $ProjectRoot -ExpectedPort $TargetPort
        if ($null -ne $archivedOwnerPath) {
            $script:ArchivedOwnerRecordPath = $archivedOwnerPath
            if (Test-Path -LiteralPath $OwnerPath) {
                Write-Result @{
                    ok = $false
                    mode = 'start'
                    started = $false
                    owned = $false
                    reused = $false
                    ownership_state = 'ownership_record_unverified'
                    archived_owner_record = $script:ArchivedOwnerRecordPath
                    service_ready = $false
                    note = 'The stale record was archived, but a new ownership record appeared before startup; no process was started.'
                }
                return
            }
            # Return to the same port and readiness guards used for a clean start.
            $portState = Get-PortOwnership -TargetPort $TargetPort
            $initialApi = Get-OllamaApiStatus -TargetPort $TargetPort -TimeoutSeconds 1
        }
        else {
            Write-Result @{
                ok = $false
                mode = 'start'
                started = $false
                owned = $false
                reused = $false
                ownership_state = 'ownership_record_unverified'
                port_owner_pids = $portState.owner_pids
                service_ready = $false
                note = 'An existing ownership record could not be verified. It was preserved and no process was started.'
            }
            return
        }
    }

    if ($initialApi.ready) {
        Write-Result @{
            ok = $true
            mode = 'start'
            started = $false
            owned = $false
            reused = $true
            ownership_state = 'not_owned'
            archived_owner_record = $script:ArchivedOwnerRecordPath
            port_owner_pids = $portState.owner_pids
            service_ready = $true
            version = $initialApi.version
            models = @($initialApi.models)
            note = 'A healthy local service is reused; no project ownership record was written.'
        }
        return
    }

    if ($portState.owner_pids.Count -gt 0) {
        Write-Result @{
            ok = $false
            mode = 'start'
            started = $false
            owned = $false
            reused = $false
            ownership_state = 'port_in_use'
            archived_owner_record = $script:ArchivedOwnerRecordPath
            port_owner_pids = $portState.owner_pids
            service_ready = $false
            note = 'The requested local port is occupied by a service that did not pass both Ollama health checks; no process was started.'
        }
        return
    }

    $homeDirectory = Join-Path $ManagedDirectory 'home'
    $tempDirectory = Join-Path $ManagedDirectory 'tmp'
    foreach ($directory in @($ManagedDirectory, $homeDirectory, $tempDirectory, $LogDirectory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }

    $logStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdoutPath = Join-Path $LogDirectory "local-ollama-$logStamp.stdout.log"
    $stderrPath = Join-Path $LogDirectory "local-ollama-$logStamp.stderr.log"
    $hostValue = "127.0.0.1:$TargetPort"
    $managedEnvironment = @{
        OLLAMA_HOST = $hostValue
        OLLAMA_MODELS = $ExpectedModelsPath
        OLLAMA_NOPRUNE = '1'
        OLLAMA_NO_CLOUD = '1'
        OLLAMA_NUM_PARALLEL = '1'
        USERPROFILE = $homeDirectory
        HOME = $homeDirectory
        TEMP = $tempDirectory
        TMP = $tempDirectory
        TMPDIR = $tempDirectory
        NO_PROXY = '*'
    }
    $clearedProxyNames = @('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')
    $previousEnvironment = @{}
    foreach ($name in @($managedEnvironment.Keys) + $clearedProxyNames) {
        $previousEnvironment[$name] = [System.Environment]::GetEnvironmentVariable($name, 'Process')
    }

    $process = $null
    try {
        foreach ($name in $clearedProxyNames) {
            [System.Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
        foreach ($name in $managedEnvironment.Keys) {
            [System.Environment]::SetEnvironmentVariable($name, [string]$managedEnvironment[$name], 'Process')
        }
        $process = Start-Process -FilePath $ExpectedExecutablePath -ArgumentList @('serve') -WorkingDirectory $homeDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    }
    finally {
        foreach ($name in $previousEnvironment.Keys) {
            [System.Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
        }
    }

    $startTimeUtcTicks = Get-ProcessStartTimeUtcTicks -ProcessId ([int]$process.Id)
    if ($null -eq $startTimeUtcTicks) { $startTimeUtcTicks = 0L }
    $ownerRecord = @{
        schema_version = 2
        manager = 'world-engine-local-ollama'
        project_root = $ProjectRoot
        pid = [int]$process.Id
        start_time_utc_ticks = [long]$startTimeUtcTicks
        executable_path = $ExpectedExecutablePath
        port = $TargetPort
        started_at = [DateTimeOffset]::UtcNow.ToString('o')
        stdout_log = $stdoutPath
        stderr_log = $stderrPath
    }
    $ownerJson = ConvertTo-Json -InputObject $ownerRecord -Depth 4
    $ownerStream = $null
    try {
        $ownerStream = [System.IO.File]::Open($OwnerPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $ownerBytes = [System.Text.UTF8Encoding]::new($false).GetBytes($ownerJson + [Environment]::NewLine)
        $ownerStream.Write($ownerBytes, 0, $ownerBytes.Length)
        $ownerStream.Flush($true)
    }
    catch {
        $script:FailureCode = 'ownership_record_write_failed'
        throw 'Could not persist project ownership metadata; the started process was left untouched.'
    }
    finally {
        if ($null -ne $ownerStream) { $ownerStream.Dispose() }
    }

    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $api = $null
    while ($clock.Elapsed.TotalSeconds -lt 30) {
        $remainingSeconds = 30 - $clock.Elapsed.TotalSeconds
        if ($remainingSeconds -lt 0.25) { break }
        $requestTimeout = [Math]::Max(0.1, [Math]::Min(1.0, $remainingSeconds / 2.0))
        $api = Get-OllamaApiStatus -TargetPort $TargetPort -TimeoutSeconds $requestTimeout
        if ($api.ready) { break }
        $remainingSeconds = 30 - $clock.Elapsed.TotalSeconds
        if ($remainingSeconds -gt 0.2) { Start-Sleep -Milliseconds 200 }
    }
    $clock.Stop()

    $finalOwnership = Test-ManagedOwnership -OwnerPath $OwnerPath -ExpectedExecutablePath $ExpectedExecutablePath -ProjectRoot $ProjectRoot -ExpectedPort $TargetPort
    $liveProcess = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    Write-Result @{
        ok = [bool]($api.ready -and $finalOwnership.owned)
        mode = 'start'
        started = $true
        owned = [bool]$finalOwnership.owned
        ownership_state = if ($finalOwnership.owned) { 'owned_verified' } elseif ($null -ne $liveProcess) { 'start_recorded_but_port_ownership_unverified' } else { 'start_recorded_process_exited' }
        pid = [int]$process.Id
        process_running = ($null -ne $liveProcess)
        service_ready = [bool]$api.ready
        readiness_wait_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 2)
        version = $api.version
        models = @($api.models)
        port_owner_pids = if ($finalOwnership.port_owner_pids) { $finalOwnership.port_owner_pids } else { @() }
        stdout_log = $stdoutPath
        stderr_log = $stderrPath
        owner_record = $OwnerPath
        archived_owner_record = $script:ArchivedOwnerRecordPath
        note = if ($api.ready -and $finalOwnership.owned) { 'Project-owned Ollama service is ready.' } else { 'Readiness did not complete within 30 seconds or the process did not own the requested port. Logs and ownership metadata were preserved; no restart was attempted.' }
    }
}

function Invoke-Stop {
    param(
        [string]$ExpectedExecutablePath,
        [string]$ProjectRoot,
        [string]$OwnerPath,
        [int]$TargetPort
    )

    $ownership = Test-ManagedOwnership -OwnerPath $OwnerPath -ExpectedExecutablePath $ExpectedExecutablePath -ProjectRoot $ProjectRoot -ExpectedPort $TargetPort
    if (-not $ownership.owned) {
        Write-Result @{
            ok = $true
            mode = 'stop'
            stopped = $false
            owned = $false
            ownership_state = if ($ownership.record_present) { 'ownership_record_unverified' } else { 'not_owned' }
            port_owner_pids = if ($ownership.port_owner_pids) { $ownership.port_owner_pids } else { @() }
            note = 'No process was stopped because this project could not verify the ownership PID, executable path, and exclusive port ownership.'
        }
        return
    }

    $record = $ownership.record
    $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
    $actualPath = Get-ResolvedProcessPath -ProcessId ([int]$record.pid)
    $actualStartTicks = Get-ProcessStartTimeUtcTicks -ProcessId ([int]$record.pid)
    $portState = Get-PortOwnership -TargetPort ([int]$record.port)
    if ($null -eq $actualPath -or $actualPath -ine [string]$record.executable_path -or $null -eq $actualStartTicks -or [long]$actualStartTicks -ne [long]$record.start_time_utc_ticks -or $portState.owner_pids.Count -ne 1 -or [int]$portState.owner_pids[0] -ne [int]$record.pid) {
        Write-Result @{
            ok = $false
            mode = 'stop'
            stopped = $false
            owned = $false
            ownership_state = 'ownership_changed_before_stop'
            port_owner_pids = $portState.owner_pids
            note = 'Ownership changed during verification; no process was stopped.'
        }
        return
    }

    Stop-Process -InputObject $process -Force -ErrorAction Stop
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(8)
    do {
        Start-Sleep -Milliseconds 200
        $stillRunning = $null -ne (Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue)
    } while ($stillRunning -and [DateTimeOffset]::UtcNow -lt $deadline)

    if ($stillRunning) {
        Write-Result @{
            ok = $false
            mode = 'stop'
            stopped = $false
            owned = $true
            pid = [int]$record.pid
            ownership_state = 'stop_requested_process_still_running'
            note = 'The explicit stop was requested, but the recorded PID is still present. Ownership metadata was preserved.'
        }
        return
    }

    $currentRecord = Read-OwnerRecord -PathValue $OwnerPath
    if ($currentRecord.valid -and [int]$currentRecord.record.pid -eq [int]$record.pid -and [long]$currentRecord.record.start_time_utc_ticks -eq [long]$record.start_time_utc_ticks -and [string]$currentRecord.record.executable_path -ieq [string]$record.executable_path -and [int]$currentRecord.record.port -eq [int]$record.port) {
        Remove-Item -LiteralPath $OwnerPath -Force
    }
    Write-Result @{
        ok = $true
        mode = 'stop'
        stopped = $true
        owned = $false
        pid = [int]$record.pid
        ownership_state = 'stopped_and_record_removed'
        note = 'The verified project-owned process exited after the explicit stop request.'
    }
}

try {
    if ($Status -and $Stop) {
        $script:FailureCode = 'invalid_mode'
        throw 'Choose either -Status or -Stop.'
    }

    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..') -ErrorAction Stop).Path
    $runtimeRoot = Join-Path $projectRoot '.runtime'
    $managedDirectory = Join-Path $runtimeRoot 'ollama-managed-service'
    $ownerPath = Join-Path $managedDirectory 'owner.json'
    $logDirectory = Join-Path $runtimeRoot 'logs'
    $resolvedExecutable = Resolve-ExistingPath -PathValue $ExecutablePath -Kind File -FailureCode 'invalid_executable_path'
    $resolvedModels = Resolve-ExistingPath -PathValue $ModelsDirectory -Kind Directory -FailureCode 'invalid_models_directory'

    if ($Status) {
        Invoke-Status -ExpectedExecutablePath $resolvedExecutable -ProjectRoot $projectRoot -OwnerPath $ownerPath -TargetPort $Port
    }
    else {
        $managerMutex = $null
        $managerMutexAcquired = $false
        try {
            $managerMutex = [System.Threading.Mutex]::new($false, 'Local\WorldEngineLocalOllamaManager')
            try {
                $managerMutexAcquired = $managerMutex.WaitOne(0)
            }
            catch [System.Threading.AbandonedMutexException] {
                $managerMutexAcquired = $true
            }
            if (-not $managerMutexAcquired) {
                $script:FailureCode = 'manager_busy'
                throw 'Another Start or Stop operation is being completed.'
            }

            if ($Stop) {
                Invoke-Stop -ExpectedExecutablePath $resolvedExecutable -ProjectRoot $projectRoot -OwnerPath $ownerPath -TargetPort $Port
            }
            else {
                Invoke-Start -ExpectedExecutablePath $resolvedExecutable -ExpectedModelsPath $resolvedModels -ProjectRoot $projectRoot -ManagedDirectory $managedDirectory -OwnerPath $ownerPath -LogDirectory $logDirectory -TargetPort $Port
            }
        }
        finally {
            if ($managerMutexAcquired -and $null -ne $managerMutex) { $managerMutex.ReleaseMutex() }
            if ($null -ne $managerMutex) { $managerMutex.Dispose() }
        }
    }
}
catch {
    Write-Result @{
        ok = $false
        mode = if ($Status) { 'status' } elseif ($Stop) { 'stop' } else { 'start' }
        error = $script:FailureCode
        detail_type = $_.Exception.GetType().Name
        archived_owner_record = if (-not [string]::IsNullOrWhiteSpace($script:ArchivedOwnerRecordPath)) { $script:ArchivedOwnerRecordPath } else { $null }
        note = 'Operation stopped safely. Exception text and environment values are not emitted.'
    }
    exit 1
}

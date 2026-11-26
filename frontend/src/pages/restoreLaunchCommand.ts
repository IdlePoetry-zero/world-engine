function quotePowerShell(value: string) {
  return `'${value.replaceAll("'", "''")}'`
}

function quoteWindowsArgument(value: string) {
  let quoted = '"'
  let backslashes = 0
  for (const character of value) {
    if (character === '\\') {
      backslashes += 1
      continue
    }
    if (character === '"') {
      quoted += '\\'.repeat(backslashes * 2 + 1) + '"'
      backslashes = 0
      continue
    }
    quoted += '\\'.repeat(backslashes) + character
    backslashes = 0
  }
  return quoted + '\\'.repeat(backslashes * 2) + '"'
}

export function buildRestoreLaunchCommand(destination: string, port: number) {
  if (!destination || !Number.isInteger(port) || port < 1 || port > 65535) return ''
  const launchArguments = `-m world_engine.runtime --data-directory ${quoteWindowsArgument(destination)} --port ${port}`
  return [
    '$worldEngineRestoreStart = New-Object System.Diagnostics.ProcessStartInfo',
    "$worldEngineRestoreStart.FileName = (Resolve-Path '.\\.runtime\\venv313\\Scripts\\python.exe').Path",
    '$worldEngineRestoreStart.WorkingDirectory = (Get-Location).Path',
    '$worldEngineRestoreStart.UseShellExecute = $false',
    "$worldEngineRestoreStart.EnvironmentVariables['WORLD_ENGINE_DATABASE_URL'] = ''",
    `$worldEngineRestoreStart.Arguments = ${quotePowerShell(launchArguments)}`,
    '$worldEngineRestoreProcess = [System.Diagnostics.Process]::Start($worldEngineRestoreStart)',
    '$worldEngineRestoreProcess.WaitForExit()',
    'if ($worldEngineRestoreProcess.ExitCode -ne 0) { Write-Error "副本启动失败，退出代码 $($worldEngineRestoreProcess.ExitCode)。若端口占用，请换用其他端口后重试。" }',
  ].join('\r\n')
}

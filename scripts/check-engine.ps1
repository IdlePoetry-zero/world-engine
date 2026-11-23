param(
    [ValidateSet('Quick', 'Rules', 'SQLite')]
    [string]$Suite = 'Quick',
    [string]$Python = (Join-Path $PSScriptRoot '../.runtime/venv313/Scripts/python.exe')
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    $versionCheck = @'
import sqlite3
import sys

print(f'Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}')
if sys.version_info[:2] != (3, 13) or sqlite3.sqlite_version_info < (3, 51, 3):
    sys.exit('Engine verification requires Python 3.13 with SQLite 3.51.3 or later.')
'@
    & $Python -c $versionCheck
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $paths = switch ($Suite) {
        'Quick' {
            'tests/domain/test_work_shift.py', 'tests/rules/test_travel_journey_rules.py',
            'tests/api/test_action_schema_size.py', 'tests/api/test_travel_chat.py', 'tests/api/test_work_chat_contract.py'
        }
        'Rules' { 'tests/agents', 'tests/content', 'tests/domain', 'tests/rules' }
        'SQLite' {
            'tests/adapters', 'tests/api', 'tests/application', 'tests/credentials', 'tests/providers',
            'tests/simulation', 'tests/storage', 'tests/test_probe_capacity_continuation.py'
        }
    }
    & $Python -m pytest -q @paths
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

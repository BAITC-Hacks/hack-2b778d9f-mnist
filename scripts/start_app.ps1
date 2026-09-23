$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$interpreter = Join-Path (Get-Location) '.venv-meeting/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $interpreter)) { throw 'Create .venv-meeting and install dependencies first; see README.' }
& $interpreter -m meeting_protocol
exit $LASTEXITCODE

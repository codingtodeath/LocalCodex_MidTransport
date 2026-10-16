param(
    [string]$Python = (Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'),
    [string]$Config = (Join-Path $env:LOCALAPPDATA 'CodexRelay\worker.json')
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
Push-Location -LiteralPath $repo
try {
    & $Python -m windows.local_control stop --config $Config
    if ($LASTEXITCODE -ne 0) { throw 'Worker did not stop cleanly; task left installed' }
    $task = Get-ScheduledTask -TaskName 'CodexRelay-Worker' -ErrorAction SilentlyContinue
    if ($task) { Unregister-ScheduledTask -TaskName 'CodexRelay-Worker' -Confirm:$false }
    Write-Output 'Uninstalled task. All local authorization and private keys preserved.'
} finally { Pop-Location }

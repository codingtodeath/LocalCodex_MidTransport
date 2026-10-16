param(
    [string]$Python = (Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'),
    [string]$Config = (Join-Path $env:LOCALAPPDATA 'CodexRelay\worker.json')
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$pythonPath = (Resolve-Path -LiteralPath (Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe')).Path
$configPath = (Resolve-Path -LiteralPath $Config).Path
if ($pythonPath.Contains('"') -or $configPath.Contains('"')) { throw 'Unsupported quote in path' }
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-m windows.local_control run --config "' + $configPath + '"') -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CodexRelay-Worker' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output 'Installed: starts at this user logon, continues while locked. Run Start-ScheduledTask -TaskName CodexRelay-Worker when ready.'

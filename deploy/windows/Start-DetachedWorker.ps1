param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Config,
    [switch]$RegisterOnly
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$consolePythonPath = Join-Path (Split-Path -Parent $pythonPath) 'python.exe'
$pythonPath = (Resolve-Path -LiteralPath (Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe')).Path
$configPath = (Resolve-Path -LiteralPath $Config).Path
if ($pythonPath.Contains('"') -or $configPath.Contains('"')) { throw 'Unsupported quote in path' }
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskName = 'CodexRelay-Worker'
$arguments = '-m windows.local_control run --config "' + $configPath + '"'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    # Preserve an explicitly installed logon trigger. Do not replace another configuration.
    if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -notin @($pythonPath, $consolePythonPath) -or
        $existing.Actions[0].Arguments -ne $arguments -or $existing.Actions[0].WorkingDirectory -ne $repo) {
        throw 'Existing worker task has a different configuration'
    }
    if ($existing.Settings.ExecutionTimeLimit -ne 'PT0S' -or $existing.State -eq 'Disabled') {
        throw 'Existing worker task has an incompatible lifetime setting'
    }
    if ($existing.Actions[0].Execute -ne $pythonPath) {
        # Changing the next action leaves the currently running task untouched.
        $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $repo
        Set-ScheduledTask -TaskName $taskName -Action $action | Out-Null
    }
} else {
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $repo
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    # Demand-start only: enabling automatic logon remains the install script's job.
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
}
if (-not $RegisterOnly) { Start-ScheduledTask -TaskName $taskName }
Write-Output '{"launch":"windows_task_scheduler","task":"CodexRelay-Worker"}'

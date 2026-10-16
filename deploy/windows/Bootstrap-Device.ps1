param(
    [string]$PairingPackage,
    [string]$Python,
    [string]$AuthFile,
    [string]$CodexExecutable,
    [string]$Proxy,
    [switch]$Replace,
    [switch]$NoConsole
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
Set-Location -LiteralPath $repo
$venv = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venv)) {
    if ($Python) { $base = $Python; $prefix = @() }
    elseif (Get-Command py.exe -ErrorAction SilentlyContinue) { $base = 'py.exe'; $prefix = @('-3') }
    elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { $base = 'python.exe'; $prefix = @() }
    else { throw 'Install 64-bit Python 3.11+ with tkinter, then run this script again.' }
    & $base @prefix -c 'import sys, tkinter; assert sys.version_info >= (3,11) and sys.maxsize > 2**32'
    if ($LASTEXITCODE -ne 0) { throw '64-bit Python 3.11+ with tkinter is required.' }
    & $base @prefix -m venv (Join-Path $repo '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project Python environment.' }
}
& $venv -m pip install --disable-pip-version-check -r (Join-Path $repo 'requirements.in')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your download proxy.' }
$arguments = @('-m', 'windows.device_setup')
if ($PairingPackage) { $arguments += @('--package', $PairingPackage) }
if ($AuthFile) { $arguments += @('--auth-file', $AuthFile) }
if ($CodexExecutable) { $arguments += @('--codex-executable', $CodexExecutable) }
if ($Proxy) { $arguments += @('--proxy', $Proxy) }
if ($Replace) { $arguments += '--replace' }
& $venv @arguments
if ($LASTEXITCODE -ne 0) { throw 'Pairing was not imported. Resolve the printed error and retry.' }
Write-Output 'Configured. Open the relay console and click Start. Closing the console does not stop the Worker.'
if (-not $NoConsole) {
    $pythonw = Join-Path $repo '.venv\Scripts\pythonw.exe'
    Start-Process -FilePath $pythonw -ArgumentList @('-m','windows.relay_console') -WorkingDirectory $repo
}

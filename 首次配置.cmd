@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if errorlevel 1 goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\Bootstrap-Device.ps1" %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo 首次配置未完成，请查看上方提示。详细说明见仓库中的 Windows 配置文档。
pause
exit /b 1

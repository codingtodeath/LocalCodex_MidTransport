@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if errorlevel 1 goto directory_error
if not exist ".venv\Scripts\pythonw.exe" goto python_error
if not exist "%LOCALAPPDATA%\CodexRelay\worker.json" goto config_error

start "" "%~dp0.venv\Scripts\pythonw.exe" -m windows.relay_console
exit /b 0

:directory_error
echo 无法进入项目目录，请从项目文件夹运行本脚本。
goto failed
:python_error
echo 找不到项目 pythonw.exe，请保留本脚本与项目 Python 环境。
goto failed
:config_error
echo 找不到当前 Windows 用户的中继配置，请使用已配置的用户运行。
:failed
pause
exit /b 1

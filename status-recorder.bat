@echo off
setlocal
chcp 65001 >nul
call "%~dp0recorder\_resolve-python.bat"
if defined RECORDER_PYTHON (
  "%RECORDER_PYTHON%" "%~dp0recorder\manage.py" status
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0recorder\status-recorder.ps1"
)
echo.
pause

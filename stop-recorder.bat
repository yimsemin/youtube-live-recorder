@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0recorder\stop-recorder.ps1"
echo.
pause

@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0status-healthcheck.ps1"
echo.
pause

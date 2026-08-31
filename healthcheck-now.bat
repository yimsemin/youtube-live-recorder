@echo off
setlocal
chcp 65001 >nul
call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHON exit /b 2
"%RECORDER_PYTHON%" "%~dp0recorder\healthcheck.py"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" echo Healthcheck: OK, ping delivered.
if "%RC%"=="1" echo Healthcheck: FAIL signal delivered. Check logs and recorder status.
if "%RC%"=="2" echo Healthcheck: ping delivery failed after retries.
pause
exit /b %RC%

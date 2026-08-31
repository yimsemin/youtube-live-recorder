@echo off
setlocal
chcp 65001 >nul
title YouTube LIVE Recorder

call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHON (
  echo ERROR: recorder\ 아래에서 이동식 Python을 찾지 못했습니다.
  pause
  exit /b 2
)

rem Arm the watchdog scheduled tasks if they are installed.
schtasks /Change /TN "YouTubeRecorder Failover Guardian" /ENABLE >nul 2>&1
schtasks /Change /TN "YouTubeRecorder Healthcheck" /ENABLE >nul 2>&1

call "%~dp0start-failover-guardian.bat"
"%RECORDER_PYTHON%" "%~dp0recorder\recorder.py" %*
set "RECORDER_EXIT=%ERRORLEVEL%"
echo.
echo Recorder exited with code %RECORDER_EXIT%.
pause
exit /b %RECORDER_EXIT%

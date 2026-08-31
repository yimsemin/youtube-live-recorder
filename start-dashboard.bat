@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title YouTube 녹화 대시보드
call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHONW (
  echo ERROR: recorder\ 아래에서 이동식 Python을 찾지 못했습니다.
  pause
  exit /b 2
)

call :ping && goto open
start "" /b "%RECORDER_PYTHONW%" "%~dp0recorder\dashboard_server.py"
for /l %%i in (1,1,20) do (
  timeout /t 1 /nobreak >nul
  call :ping && goto open
)
echo 대시보드가 응답하지 않습니다. logs\dashboard.log 를 확인하세요.
pause
exit /b 1

:open
start "" http://127.0.0.1:8787/
echo 대시보드: http://127.0.0.1:8787/
exit /b 0

:ping
powershell -NoProfile -Command "try { if ((Invoke-RestMethod 'http://127.0.0.1:8787/health' -TimeoutSec 2).ok) { exit 0 } } catch {}; exit 1"
exit /b %ERRORLEVEL%

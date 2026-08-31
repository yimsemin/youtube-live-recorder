@echo off
setlocal
chcp 65001 >nul
set "PIDFILE=%~dp0config\dashboard-api.pid"
if not exist "%PIDFILE%" (
  echo 대시보드가 실행 중이 아닙니다.
  exit /b 0
)
set /p DASHPID=<"%PIDFILE%"
taskkill /PID %DASHPID% /T /F >nul 2>&1
del "%PIDFILE%" >nul 2>&1
echo 대시보드를 종료했습니다. 녹화기는 그대로 둡니다.

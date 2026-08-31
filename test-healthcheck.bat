@echo off
setlocal
chcp 65001 >nul
call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHON exit /b 2
"%RECORDER_PYTHON%" "%~dp0recorder\healthcheck.py" --dry-run
echo.
echo 로컬 점검만 수행했습니다. 외부 ping은 보내지 않았고 상태도 바꾸지 않았습니다.
pause

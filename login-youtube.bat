@echo off
setlocal
title YouTube 로그인
call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHON (
  echo ERROR: recorder\ 아래에서 이동식 Python을 찾지 못했습니다.
  pause
  exit /b 2
)
"%RECORDER_PYTHON%" "%~dp0recorder\auth_login.py"
echo.
pause
exit /b %ERRORLEVEL%

@echo off
setlocal
chcp 65001 >nul
title YouTube LIVE Backfill
call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHON exit /b 2
"%RECORDER_PYTHON%" "%~dp0recorder\backfill.py"
echo.
pause
exit /b %ERRORLEVEL%

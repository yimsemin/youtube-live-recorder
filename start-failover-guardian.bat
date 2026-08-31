@echo off
setlocal
call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHONW exit /b 2
if exist "%~dp0config\failover.disabled" del /q "%~dp0config\failover.disabled"
if exist "%~dp0config\failover.stop.request" del /q "%~dp0config\failover.stop.request"
start "" /b "%RECORDER_PYTHONW%" "%~dp0recorder\failover_guardian.py"
exit /b 0

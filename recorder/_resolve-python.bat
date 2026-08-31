@echo off
rem Sets RECORDER_PYTHON / RECORDER_PYTHONW for the caller. Call, do not run directly.
set "RECORDER_PYTHON="
set "RECORDER_PYTHONW="
if exist "%~dp0python\python.exe"  set "RECORDER_PYTHON=%~dp0python\python.exe"
if exist "%~dp0python\pythonw.exe" set "RECORDER_PYTHONW=%~dp0python\pythonw.exe"
for /d %%D in ("%~dp0streamlink-*-x86_64") do (
  if exist "%%~fD\Python\python.exe"  set "RECORDER_PYTHON=%%~fD\Python\python.exe"
  if exist "%%~fD\Python\pythonw.exe" set "RECORDER_PYTHONW=%%~fD\Python\pythonw.exe"
)

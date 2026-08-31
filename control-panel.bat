@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title YouTube 녹화 제어판
call "%~dp0recorder\_resolve-python.bat"
if not defined RECORDER_PYTHON (
  echo ERROR: recorder\ 아래에서 이동식 Python을 찾지 못했습니다.
  pause
  exit /b 2
)

:menu
cls
echo ============================================
echo        YouTube LIVE 녹화 제어판
echo ============================================
echo   1. 상태 보기 (모든 프로세스)
echo   2. 녹화 시작
echo   3. 녹화 안전 종료 (현재 MKV 마감)
echo   4. 전체 종료 (녹화기 + 감시기 + 대시보드)
echo   5. 대시보드 열기
echo   6. YouTube 로그인
echo   0. 닫기
echo ============================================
set "choice="
set /p choice=번호 선택:

if "%choice%"=="1" ( "%RECORDER_PYTHON%" "%~dp0recorder\manage.py" status & pause & goto menu )
if "%choice%"=="2" ( call "%~dp0start-recorder.bat" & goto menu )
if "%choice%"=="3" ( call "%~dp0stop-recorder.bat" & goto menu )
if "%choice%"=="4" ( "%RECORDER_PYTHON%" "%~dp0recorder\manage.py" stop-all & pause & goto menu )
if "%choice%"=="5" ( call "%~dp0start-dashboard.bat" & goto menu )
if "%choice%"=="6" ( call "%~dp0login-youtube.bat" & goto menu )
if "%choice%"=="0" ( exit /b 0 )
goto menu

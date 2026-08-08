@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "STW_PYTHON=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 was not found. Install Python 3 and try again.
    pause
    exit /b 1
  )
  set "STW_PYTHON=python"
)

start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765'"
%STW_PYTHON% tools\stw_app.py
if errorlevel 1 (
  echo.
  echo STW Intelligence stopped with an error. Run diagnostics with:
  echo %STW_PYTHON% tools\stw_admin.py diagnostics
  pause
)

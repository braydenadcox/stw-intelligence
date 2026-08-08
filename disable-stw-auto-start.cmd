@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "tools\install_auto_start.ps1" -Remove
if errorlevel 1 pause

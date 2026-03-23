@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup-Full.ps1"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   Setup encountered an error. Press any key to close.
    pause >nul
)

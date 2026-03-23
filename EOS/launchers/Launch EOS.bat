@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
powershell -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
pause

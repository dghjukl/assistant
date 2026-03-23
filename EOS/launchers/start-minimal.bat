@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching main model (GPU)
echo [EOS] Tool / thinking / creativity start on-demand via the WebUI
start "EOS main (GPU)" cmd /k "%ROOT%\launchers\start-main-gpu.bat"

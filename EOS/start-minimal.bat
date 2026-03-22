@echo off
cd /d "%~dp0"
echo [EOS] Launching main model (GPU)
echo [EOS] Tool / thinking / creativity start on-demand via the WebUI
start "EOS main (GPU)" cmd /k "%~dp0start-main-gpu.bat"

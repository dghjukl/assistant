@echo off
cd /d "%~dp0"
echo [EOS] Launching minimal backend bundle (main + tools)
start "EOS main (GPU)" cmd /k "\"%~dp0start-main-gpu.bat\""
start "EOS tools (CPU)" cmd /k "\"%~dp0start-tools-cpu.bat\""

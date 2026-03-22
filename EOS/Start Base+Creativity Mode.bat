@echo off
title EOS — Main + Tools + Creativity
cd /d "%~dp0"
start "EOS main (GPU)" cmd /k "\"%~dp0start-main-gpu.bat\""
start "EOS tools (CPU)" cmd /k "\"%~dp0start-tools-cpu.bat\""
start "EOS creativity (CPU)" cmd /k "\"%~dp0start-creativity-cpu.bat\""
call "%~dp0start-eos.bat"

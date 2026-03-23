@echo off
title EOS — Main + Tools + Creativity
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
start "EOS main (GPU)" cmd /k "\"%ROOT%\launchers\start-main-gpu.bat\""
start "EOS tools (CPU)" cmd /k "\"%ROOT%\launchers\start-tools-cpu.bat\""
start "EOS creativity (CPU)" cmd /k "\"%ROOT%\launchers\start-creativity-cpu.bat\""
call "%ROOT%\start-eos.bat"

@echo off
title EOS — Vision Mode
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
call "%ROOT%\launchers\start-standard.bat"
start "EOS vision (GPU)" cmd /k "\"%ROOT%\launchers\start-vision-gpu.bat\""
call "%ROOT%\start-eos.bat"

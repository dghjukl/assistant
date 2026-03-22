@echo off
title EOS — Vision Mode
cd /d "%~dp0"
call "%~dp0start-standard.bat"
start "EOS vision (GPU)" cmd /k "\"%~dp0start-vision-gpu.bat\""
call "%~dp0start-eos.bat"

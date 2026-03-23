@echo off
title EOS — Standard Mode
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
call "%ROOT%\launchers\start-standard.bat"
call "%ROOT%\start-eos.bat"

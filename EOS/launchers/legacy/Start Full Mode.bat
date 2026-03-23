@echo off
title EOS — Full Mode
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
call "%ROOT%\launchers\start-full.bat"
call "%ROOT%\start-eos.bat"

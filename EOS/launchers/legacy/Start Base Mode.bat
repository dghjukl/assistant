@echo off
title EOS — Minimal Mode
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
call "%ROOT%\launchers\start-minimal.bat"
call "%ROOT%\start-eos.bat"

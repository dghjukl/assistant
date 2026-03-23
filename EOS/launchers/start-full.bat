@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching full mode (main model only — all helpers start on-demand)
call "%ROOT%\launchers\start-minimal.bat"

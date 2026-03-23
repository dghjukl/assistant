@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching standard mode (main model only — helpers start on-demand)
call "%ROOT%\launchers\start-minimal.bat"

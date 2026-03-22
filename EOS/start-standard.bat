@echo off
cd /d "%~dp0"
echo [EOS] Launching standard mode (main model only — helpers start on-demand)
call "%~dp0start-minimal.bat"

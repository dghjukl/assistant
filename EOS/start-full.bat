@echo off
cd /d "%~dp0"
echo [EOS] Launching full mode (main model only — all helpers start on-demand)
call "%~dp0start-minimal.bat"

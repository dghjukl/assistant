@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching full profile (resident main, tools, thinking, creativity, and vision when launchable)
python -m runtime.launch_profile full --root "%ROOT%" --config "%ROOT%\config.json"

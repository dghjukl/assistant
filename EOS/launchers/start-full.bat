@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching full profile (resident baseline services; auxiliary cognition remains elastic/on-demand)
python -m runtime.launch_profile full --root "%ROOT%" --config "%ROOT%\config.json"

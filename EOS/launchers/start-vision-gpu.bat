@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching hardened vision profile
python -m runtime.launch_profile vision --root "%ROOT%" --config "%ROOT%\config.json"

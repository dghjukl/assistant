@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching hardened minimal profile (main model only)
python -m runtime.launch_profile minimal --root "%ROOT%" --config "%ROOT%\config.json"

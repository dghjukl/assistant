@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching standard profile (main + tools + thinking)
python -m runtime.launch_profile standard --root "%ROOT%" --config "%ROOT%\config.json"

@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Launching hardened full profile (main + tools + thinking + creativity)
python -m runtime.launch_profile full --root "%ROOT%" --config "%ROOT%\config.json"

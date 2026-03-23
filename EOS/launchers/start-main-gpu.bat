@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Starting main model on GPU at http://127.0.0.1:8080/
python -m runtime.server_launcher main --accel gpu --config "%ROOT%\config.json"

@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Starting tool helper on GPU at http://127.0.0.1:8082/
python -m runtime.server_launcher tools --accel gpu --config "%ROOT%\config.json"

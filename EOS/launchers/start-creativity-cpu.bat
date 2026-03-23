@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Starting creativity helper on CPU at http://127.0.0.1:8084/
python -m runtime.server_launcher creativity --accel cpu --config "%ROOT%\config.json"

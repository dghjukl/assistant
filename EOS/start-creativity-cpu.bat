@echo off
cd /d "%~dp0"
echo [EOS] Starting creativity helper on CPU at http://127.0.0.1:8084/
python -m runtime.server_launcher creativity --accel cpu --config config.json

@echo off
cd /d "%~dp0"
echo [EOS] Starting creativity helper on GPU at http://127.0.0.1:8084/
python -m runtime.server_launcher creativity --accel gpu --config config.json

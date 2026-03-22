@echo off
cd /d "%~dp0"
echo [EOS] Starting vision helper on GPU at http://127.0.0.1:8081/
python -m runtime.server_launcher vision --accel gpu --config config.json

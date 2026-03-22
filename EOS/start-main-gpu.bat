@echo off
cd /d "%~dp0"
echo [EOS] Starting main model on GPU at http://127.0.0.1:8080/
python -m runtime.server_launcher main --accel gpu --config config.json

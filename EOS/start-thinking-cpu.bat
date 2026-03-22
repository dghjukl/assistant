@echo off
cd /d "%~dp0"
echo [EOS] Starting thinking helper on CPU at http://127.0.0.1:8083/
python -m runtime.server_launcher thinking --accel cpu --config config.json

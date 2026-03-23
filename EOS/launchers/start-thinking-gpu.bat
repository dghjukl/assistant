@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo [EOS] Starting thinking helper on GPU at http://127.0.0.1:8083/
python -m runtime.server_launcher thinking --accel gpu --config "%ROOT%\config.json"

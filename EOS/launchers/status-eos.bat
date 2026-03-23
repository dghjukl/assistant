@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
python "%ROOT%\eos.py" --status --config "%ROOT%\config.json"
pause

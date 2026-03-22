@echo off
cd /d "%~dp0"
python eos.py --status --config config.json
pause

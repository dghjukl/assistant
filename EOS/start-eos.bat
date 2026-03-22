@echo off
title EOS — Bootstrap
cd /d "%~dp0"
echo.
echo  ====================================================
echo    EOS  ^|  Bootstrap
echo  ====================================================
echo    Discovering running services from config.json
echo    WebUI: http://127.0.0.1:7860/
echo    Admin: http://127.0.0.1:7860/admin
echo  ====================================================
echo.
python eos.py --config config.json
pause

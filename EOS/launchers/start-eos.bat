@echo off
title EOS — Bootstrap
set "ROOT=%~dp0.."
cd /d "%ROOT%"
echo.
echo  ====================================================
echo    EOS  ^|  Bootstrap
echo  ====================================================
echo    Discovering running services from config.json
echo    WebUI: http://127.0.0.1:7860/
echo    Admin: http://127.0.0.1:7860/admin
echo  ====================================================
echo.
python "%ROOT%\eos.py" --config "%ROOT%\config.json"
pause

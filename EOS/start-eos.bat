@echo off
title EOS — Bootstrap
cd /d "%~dp0"
echo.
echo  ====================================================
echo    EOS ^| Bootstrap
echo  ====================================================
echo    EOS is the platform
echo    The entity is the runtime intelligence inside EOS
echo    The entity name is not the product name
echo  ----------------------------------------------------
echo    Discovering running services from config.json
echo    Recommended backend default: launchers\start-standard.bat
echo    WebUI: http://127.0.0.1:7860/
echo    Admin: http://127.0.0.1:7860/admin
echo  ====================================================
echo.
python eos.py --config config.json
pause

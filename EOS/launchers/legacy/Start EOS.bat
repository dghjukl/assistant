@echo off
title EOS — Startup Menu
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"

:menu
cls
echo.
echo  ============================================================
echo    EOS  ^|  Startup Menu
echo  ============================================================
echo.
echo    1.  Base Mode          ^|  Main + Tools  (lightest^)
echo    2.  Standard Mode      ^|  Main + Tools + Thinking
echo    3.  Creativity Mode    ^|  Main + Tools + Creativity
echo    4.  Full Mode          ^|  Main + Tools + Thinking + Creativity
echo    5.  Vision Mode        ^|  Main + Tools + Thinking + Vision
echo    6.  No-Boot Mode       ^|  WebUI only  (servers already running^)
echo.
echo  ============================================================
echo.

choice /c 123456 /n /m "  Select a mode [1-6]: "

if errorlevel 6 goto noboot
if errorlevel 5 goto vision
if errorlevel 4 goto full
if errorlevel 3 goto creativity
if errorlevel 2 goto standard
if errorlevel 1 goto base

:base
echo.
echo  [EOS] Starting Base Mode (Main + Tools)...
call "%ROOT%\launchers\start-minimal.bat"
call "%ROOT%\start-eos.bat"
goto end

:standard
echo.
echo  [EOS] Starting Standard Mode (Main + Tools + Thinking)...
call "%ROOT%\launchers\start-standard.bat"
call "%ROOT%\start-eos.bat"
goto end

:creativity
echo.
echo  [EOS] Starting Creativity Mode (Main + Tools + Creativity)...
start "EOS main (GPU)"       cmd /k "\"%ROOT%\launchers\start-main-gpu.bat\""
start "EOS tools (CPU)"      cmd /k "\"%ROOT%\launchers\start-tools-cpu.bat\""
start "EOS creativity (CPU)" cmd /k "\"%ROOT%\launchers\start-creativity-cpu.bat\""
call "%ROOT%\start-eos.bat"
goto end

:full
echo.
echo  [EOS] Starting Full Mode (Main + Tools + Thinking + Creativity)...
call "%ROOT%\launchers\start-full.bat"
call "%ROOT%\start-eos.bat"
goto end

:vision
echo.
echo  [EOS] Starting Vision Mode (Main + Tools + Thinking + Vision)...
call "%ROOT%\launchers\start-standard.bat"
start "EOS vision (GPU)" cmd /k "\"%ROOT%\launchers\start-vision-gpu.bat\""
call "%ROOT%\start-eos.bat"
goto end

:noboot
echo.
echo  [EOS] Starting No-Boot Mode (WebUI only)...
call "%ROOT%\start-eos.bat"
goto end

:end

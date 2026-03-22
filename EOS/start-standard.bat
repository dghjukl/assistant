@echo off
cd /d "%~dp0"
echo [EOS] Launching standard backend bundle (main + tools + thinking)
call "%~dp0start-minimal.bat"
start "EOS thinking (CPU)" cmd /k "\"%~dp0start-thinking-cpu.bat\""

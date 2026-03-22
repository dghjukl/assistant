@echo off
cd /d "%~dp0"
echo [EOS] Launching full backend bundle (main + tools + thinking + creativity)
call "%~dp0start-standard.bat"
start "EOS creativity (CPU)" cmd /k "\"%~dp0start-creativity-cpu.bat\""

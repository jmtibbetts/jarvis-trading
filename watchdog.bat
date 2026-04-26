@echo off
:: Jarvis Watchdog — auto-restarts if it crashes
:loop
echo [%date% %time%] Starting Jarvis...
call start.bat
echo [%date% %time%] Jarvis stopped — restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop

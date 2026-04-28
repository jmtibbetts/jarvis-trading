@echo off
setlocal
cd /d "%~dp0"
echo.
echo  ============================================================
echo   Jarvis Watchdog — auto-restart on crash
echo  ============================================================
echo  Press Ctrl+C to stop the watchdog.
echo.

:loop
echo  [%date% %time%] Starting Jarvis...
call .venv\Scripts\activate.bat
python main.py
echo.
echo  [%date% %time%] Jarvis stopped (exit code %errorlevel%) — restarting in 10s...
echo  Press Ctrl+C within 10s to abort.
timeout /t 10 /nobreak >nul
goto loop

endlocal

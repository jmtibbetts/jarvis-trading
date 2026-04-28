@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  ============================================================
echo   Jarvis Trading AI v6.1  [Windows]
echo  ============================================================
echo.

:: ── Check Python 3.12 ─────────────────────────────────────────
py -3.12 --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python 3.12 not found.
    echo  Download from: https://www.python.org/downloads/release/python-3120/
    echo  Make sure to check "Add Python to PATH" during install.
    pause & exit /b 1
)
for /f "tokens=*" %%v in ('py -3.12 --version 2^>^&1') do echo  Found: %%v

:: ── Create or reuse .venv ─────────────────────────────────────
if not exist .venv (
    echo.
    echo  Creating virtual environment with Python 3.12...
    py -3.12 -m venv .venv
    if %errorlevel% neq 0 (
        echo  ERROR: Could not create .venv
        pause & exit /b 1
    )
    echo  Virtual environment created.
)
call .venv\Scripts\activate.bat

:: ── Smart dependency check ────────────────────────────────────
:: Only run pip install if requirements.txt changed since last install,
:: or if the sentinel file doesn't exist yet.
echo.
set SENTINEL=.venv\.deps_installed
set NEEDS_INSTALL=0

if not exist "%SENTINEL%" (
    set NEEDS_INSTALL=1
    echo  First run — installing dependencies...
) else (
    :: Compare requirements.txt modified date to sentinel date
    for /f %%a in ('forfiles /p "%~dp0" /m requirements.txt /c "cmd /c echo @fdate @ftime" 2^>nul') do set REQ_DATE=%%a
    for /f %%a in ('forfiles /p "%~dp0\.venv" /m .deps_installed /c "cmd /c echo @fdate @ftime" 2^>nul') do set SENT_DATE=%%a
    if "!REQ_DATE!" neq "!SENT_DATE!" (
        set NEEDS_INSTALL=1
        echo  requirements.txt changed — updating dependencies...
    ) else (
        echo  Dependencies up to date ^(delete .venv\.deps_installed to force reinstall^).
    )
)

if !NEEDS_INSTALL! equ 1 (
    python -m pip install --upgrade pip --quiet
    pip install -r requirements.txt --quiet
    if %errorlevel% neq 0 (
        echo  ERROR: Dependency install failed. Run with verbose:
        echo    pip install -r requirements.txt
        pause & exit /b 1
    )
    :: Write sentinel with same timestamp logic (just touch the file)
    echo installed > "%SENTINEL%"
    echo  Dependencies installed OK.
)

:: ── TA-Lib (pre-built wheel for Python 3.12 / Win x64) ───────
echo.
echo  Checking TA-Lib...
python -c "import talib" 2>nul
if %errorlevel% neq 0 (
    echo  TA-Lib not found. Downloading pre-built wheel...
    set TALIB_WHL=ta_lib-0.6.8-cp312-cp312-win_amd64.whl
    set TALIB_URL=https://github.com/cgohlke/talib-build/releases/download/v0.6.8/ta_lib-0.6.8-cp312-cp312-win_amd64.whl
    if not exist !TALIB_WHL! (
        curl -L --progress-bar -o !TALIB_WHL! !TALIB_URL!
        if %errorlevel% neq 0 (
            echo  WARNING: Could not download TA-Lib wheel. Falling back to 'ta'...
            pip install ta==0.11.0 --quiet
            goto talib_done
        )
    )
    pip install !TALIB_WHL!
    if %errorlevel% neq 0 (
        echo  TA-Lib wheel install failed. Falling back to 'ta'...
        pip install ta==0.11.0 --quiet
    ) else (
        echo  TA-Lib 0.6.8 installed successfully!
        echo installed > "%SENTINEL%"
    )
) else (
    echo  TA-Lib already installed.
)
:talib_done

:: ── Data directory ────────────────────────────────────────────
if not exist data mkdir data

:: ── .env check ───────────────────────────────────────────────
if not exist .env (
    echo.
    echo  NOTE: No .env file found.
    if exist .env.example (
        copy .env.example .env >nul
        echo  Created .env from .env.example — edit it with your API keys.
    ) else (
        echo  Create a .env file with your ALPACA_API_KEY, etc.
    )
    echo.
)

:: ── Launch ────────────────────────────────────────────────────
echo.
echo  Starting Jarvis at http://localhost:3000 ...
echo  Press Ctrl+C to stop.
echo.

:: Open browser after 3 second delay
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:3000"

python main.py

endlocal
pause

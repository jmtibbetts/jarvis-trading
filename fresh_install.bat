@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ============================================================
echo   Jarvis Trading AI v6.1  [Windows — Fresh Install]
echo  ============================================================
echo.
echo  This will:
echo    1. Pull latest code from GitHub (if in a git repo)
echo    2. Delete the existing .venv
echo    3. Create a fresh .venv using Python 3.12
echo    4. Install all dependencies (including TA-Lib)
echo    5. Start Jarvis
echo.
echo  WARNING: Any local .env changes are safe — only .venv is deleted.
echo.
pause

:: Step 1 — git pull
echo.
echo  [1/5] Pulling latest code...
git pull 2>nul || echo  (No git repo or git not installed — skipping)

:: Step 2 — delete venv
echo.
echo  [2/5] Removing old virtual environment...
if exist .venv (
    rmdir /s /q .venv
    echo  Removed .venv
) else (
    echo  No .venv found — skipping
)

:: Step 3 — create venv
echo.
echo  [3/5] Creating fresh virtual environment with Python 3.12...
py -3.12 -m venv .venv
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: py -3.12 not found. Install Python 3.12 from:
    echo    https://www.python.org/downloads/release/python-3120/
    pause & exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet

:: Step 4 — install deps
echo.
echo  [4/5] Installing dependencies (2-3 min first time)...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo  ERROR: pip install failed.
    pause & exit /b 1
)

:: TA-Lib wheel
set TALIB_WHL=ta_lib-0.6.8-cp312-cp312-win_amd64.whl
set TALIB_URL=https://github.com/cgohlke/talib-build/releases/download/v0.6.8/ta_lib-0.6.8-cp312-cp312-win_amd64.whl
if not exist !TALIB_WHL! (
    echo  Downloading TA-Lib wheel...
    curl -L --progress-bar -o !TALIB_WHL! !TALIB_URL!
)
pip install !TALIB_WHL! 2>nul || pip install ta==0.11.0

if not exist data mkdir data
if not exist .env (
    if exist .env.example copy .env.example .env >nul
    echo  Created .env from template — edit with your API keys.
)

:: Step 5 — launch
echo.
echo  [5/5] Starting Jarvis...
echo.
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:3000"
python main.py

endlocal
pause

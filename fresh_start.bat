@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ============================================================
echo   Jarvis Trading AI - Clean Install Script
echo  ============================================================
echo.
echo  This will:
echo    1. Pull latest code from GitHub
echo    2. Delete the old broken .venv
echo    3. Create a fresh .venv using Python 3.13
echo    4. Install all dependencies
echo    5. Start Jarvis
echo.
pause

:: Step 1 - Git pull
echo.
echo  [1/5] Pulling latest code...
git pull
if %errorlevel% neq 0 (
    echo  WARNING: git pull failed - continuing with existing files
)

:: Step 2 - Delete old venv
echo.
echo  [2/5] Removing old virtual environment...
if exist .venv (
    rmdir /s /q .venv
    echo  Removed .venv
) else (
    echo  No .venv found, skipping
)

:: Step 3 - Create fresh venv with Python 3.13
echo.
echo  [3/5] Creating fresh virtual environment with Python 3.13...
py -3.13 -m venv .venv
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Could not create venv with py -3.13
    echo  Make sure Python 3.13 is installed from python.org
    echo  Then run: py -0  to verify it shows up
    pause
    exit /b 1
)

:: Step 4 - Activate and install
echo.
echo  [4/5] Installing dependencies (this takes 2-3 minutes)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Dependency install failed. See errors above.
    pause
    exit /b 1
)

:: Create data dir
if not exist data mkdir data

:: Step 5 - Start
echo.
echo  [5/5] Starting Jarvis...
echo.
python main.py

endlocal
pause

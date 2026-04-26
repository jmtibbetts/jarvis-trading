@echo off
setlocal

echo.
echo  Jarvis Trading AI v5.0 - Python Edition
echo  ========================================
echo.

:: Check Python
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)

:: Delete broken venv if activate script is missing
if exist .venv (
    if not exist .venv\Scripts\activate.bat (
        echo  Removing broken venv...
        rmdir /s /q .venv
    )
)

:: Create venv if missing
if not exist .venv (
    echo  Creating virtual environment...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo  ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: Activate venv
echo  Activating virtual environment...
call .venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo  ERROR: Could not activate venv. Try deleting the .venv folder and re-running.
    pause
    exit /b 1
)

:: Verify pip works
python -m pip --version > nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: pip not available inside venv.
    pause
    exit /b 1
)

:: Install dependencies
echo  Installing dependencies...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Dependency install failed. See errors above.
    pause
    exit /b 1
)

:: Create data dir
if not exist data mkdir data

echo.
echo  Starting Jarvis...
echo.
python main.py

endlocal
pause

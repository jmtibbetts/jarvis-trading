@echo off
echo.
echo  Jarvis Trading AI v5.0 — Python Edition
echo  ========================================
echo.

:: Check Python
python --version > nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Install Python 3.11+ from python.org
    pause
    exit /b 1
)

:: Create venv if missing
if not exist .venv (
    echo  Creating virtual environment...
    python -m venv .venv
)

:: Activate + install
call .venv\Scripts\activate.bat

echo  Checking dependencies...
pip install -q -r requirements.txt

:: Create data dir
if not exist data mkdir data

echo  Starting Jarvis...
python main.py
pause

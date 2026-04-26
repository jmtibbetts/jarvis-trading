@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo  ============================================================
echo   Jarvis Trading AI v6.1
echo  ============================================================
echo.

:: Activate venv
if not exist .venv\Scripts\activate.bat (
    echo  ERROR: .venv not found. Run: py -3.12 -m venv .venv
    pause & exit /b 1
)
call .venv\Scripts\activate.bat

:: Upgrade pip
python -m pip install --upgrade pip --quiet

:: Install base requirements
echo  Installing base dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo  ERROR: Base dependency install failed.
    pause & exit /b 1
)

:: Try TA-Lib
echo.
echo  Checking for TA-Lib...
python -c "import talib" 2>nul
if %errorlevel% neq 0 (
    echo  TA-Lib not found. Installing pre-built wheel for Python 3.12 / Windows x64...
    set TALIB_WHL=ta_lib-0.6.8-cp312-cp312-win_amd64.whl
    set TALIB_URL=https://github.com/cgohlke/talib-build/releases/download/v0.6.8/ta_lib-0.6.8-cp312-cp312-win_amd64.whl

    if not exist !TALIB_WHL! (
        echo  Downloading...
        curl -L -o !TALIB_WHL! !TALIB_URL!
    )

    echo  Installing TA-Lib wheel...
    pip install !TALIB_WHL!
    if %errorlevel% neq 0 (
        echo.
        echo  TA-Lib wheel failed. Installing ta fallback...
        pip install ta==0.11.0
        if %errorlevel% neq 0 (
            echo  ERROR: Could not install any TA library.
            pause & exit /b 1
        )
        echo  Using ta==0.11.0 (pure Python fallback)
    ) else (
        echo  TA-Lib 0.6.8 installed successfully!
    )
) else (
    echo  TA-Lib already installed.
)

:: Create data dir
if not exist data mkdir data

:: Launch
echo.
echo  Starting Jarvis...
echo.
python main.py

endlocal
pause

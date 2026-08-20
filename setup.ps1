# Jarvis Trading AI v6.1 - Windows PowerShell Setup
# Creates .venv, installs deps, creates Desktop shortcut
param([string]$RootDir = $PSScriptRoot)
if (-not $RootDir) { $RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path }

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "   Jarvis Trading AI v6.1  [PowerShell Setup]" -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host ""

# Check Python 3.12
$py = Get-Command "py" -ErrorAction SilentlyContinue
if ($null -eq $py) {
    Write-Host "  ERROR: 'py' launcher not found. Install Python 3.12 from python.org" -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}
$ver = & py -3.12 --version 2>&1
Write-Host "  Found: $ver" -ForegroundColor Green

# Create venv
$venv = Join-Path $RootDir ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "  Creating .venv..." -ForegroundColor Yellow
    & py -3.12 -m venv $venv
}

# THE INTERPRETER IS NAMED, NOT INHERITED FROM PATH.
#
# This used to activate the venv and then call bare `python` and `pip`.
# That works only while activation succeeded: `Activate.ps1` can fail
# silently under a restrictive ExecutionPolicy, and this script does not
# set $ErrorActionPreference, so execution continues either way. The next
# bare `python` then resolves to whatever is first on PATH -- on this
# machine that is the Microsoft Store shim in
# %LOCALAPPDATA%\Microsoft\WindowsApps -- and every dependency lands in
# the wrong interpreter while the output still reads like success.
#
# Naming the interpreter removes the dependency on activation entirely.
$VenvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "  ERROR: no interpreter at $VenvPython after venv creation." -ForegroundColor Red
    Write-Host "  Refusing to install into an unknown Python." -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}

# Activation is kept for the interactive shell it leaves behind; nothing
# below depends on it.
& "$venv\Scripts\Activate.ps1"
& $VenvPython -m pip install --upgrade pip --quiet
Write-Host "  Installing dependencies..." -ForegroundColor Yellow
& $VenvPython -m pip install -r (Join-Path $RootDir "requirements.txt") --quiet

# TA-Lib wheel
$talibCheck = & $VenvPython -c "import talib" 2>&1
if ($LASTEXITCODE -ne 0) {
    $wheel = Join-Path $RootDir "ta_lib-0.6.8-cp312-cp312-win_amd64.whl"
    if (-not (Test-Path $wheel)) {
        Write-Host "  Downloading TA-Lib wheel..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "https://github.com/cgohlke/talib-build/releases/download/v0.6.8/ta_lib-0.6.8-cp312-cp312-win_amd64.whl" -OutFile $wheel
    }
    & $VenvPython -m pip install $wheel --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  TA-Lib wheel failed - falling back to 'ta'" -ForegroundColor Yellow
        & $VenvPython -m pip install ta==0.11.0 --quiet
    } else {
        Write-Host "  TA-Lib 0.6.8 installed!" -ForegroundColor Green
    }
} else {
    Write-Host "  TA-Lib already installed." -ForegroundColor Green
}

# Data dir + .env
$dataDir = Join-Path $RootDir "data"
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$envFile = Join-Path $RootDir ".env"
if (-not (Test-Path $envFile)) {
    $envExample = Join-Path $RootDir ".env.example"
    if (Test-Path $envExample) { Copy-Item $envExample $envFile }
    Write-Host "  Created .env from template - edit with your API keys." -ForegroundColor Yellow
}

# Desktop shortcut
try {
    $shell    = New-Object -ComObject WScript.Shell
    $lnkPath  = "$env:USERPROFILE\Desktop\Jarvis Trading AI.lnk"
    $shortcut = $shell.CreateShortcut($lnkPath)
    $shortcut.TargetPath     = "powershell.exe"
    $shortcut.Arguments      = "-ExecutionPolicy Bypass -File `"$RootDir\start.ps1`""
    $shortcut.WorkingDirectory = $RootDir
    $shortcut.Description    = "Jarvis Trading AI v6.1"
    $shortcut.Save()
    Write-Host "  Desktop shortcut created: Jarvis Trading AI" -ForegroundColor Green
} catch {
    Write-Host "  Could not create desktop shortcut (non-fatal): $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "  Run:  .\start.bat   or double-click the Desktop shortcut" -ForegroundColor White
Write-Host ""
Read-Host "Press Enter to exit"

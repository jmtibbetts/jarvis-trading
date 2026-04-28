# Jarvis Trading AI v6.1 — Windows PowerShell Setup
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

# Activate + install
& "$venv\Scripts\Activate.ps1"
& python -m pip install --upgrade pip --quiet
Write-Host "  Installing dependencies..." -ForegroundColor Yellow
& pip install -r (Join-Path $RootDir "requirements.txt") --quiet

# TA-Lib wheel
$talibCheck = python -c "import talib" 2>&1
if ($LASTEXITCODE -ne 0) {
    $wheel = Join-Path $RootDir "ta_lib-0.6.8-cp312-cp312-win_amd64.whl"
    if (-not (Test-Path $wheel)) {
        Write-Host "  Downloading TA-Lib wheel..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "https://github.com/cgohlke/talib-build/releases/download/v0.6.8/ta_lib-0.6.8-cp312-cp312-win_amd64.whl" -OutFile $wheel
    }
    & pip install $wheel --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  TA-Lib wheel failed — falling back to 'ta'" -ForegroundColor Yellow
        & pip install ta==0.11.0 --quiet
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
    Write-Host "  Created .env from template — edit with your API keys." -ForegroundColor Yellow
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

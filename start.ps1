# Jarvis Trading AI v6.1 - PowerShell Launcher
param([string]$RootDir = $PSScriptRoot)
if (-not $RootDir) { $RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $RootDir

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "   Jarvis Trading AI v6.1  [Windows PowerShell]" -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host ""

$venv = Join-Path $RootDir ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venv)) {
    Write-Host "  .venv not found - running setup first..." -ForegroundColor Yellow
    & powershell.exe -ExecutionPolicy Bypass -File (Join-Path $RootDir "setup.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Setup failed. Run setup.ps1 manually." -ForegroundColor Red
        Read-Host "Press Enter to exit"; exit 1
    }
}

if (-not (Test-Path $venvPython)) {
    Write-Host "  ERROR: .venv exists but Python is missing at $venvPython" -ForegroundColor Red
    Write-Host "  Run .\fresh_install.bat or delete .venv and run .\start.bat once." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"; exit 1
}

$sentinel = Join-Path $venv ".deps_installed"
$requirements = Join-Path $RootDir "requirements.txt"
$needsInstall = $false

if (-not (Test-Path $sentinel)) {
    $needsInstall = $true
    Write-Host "  First PowerShell run - installing dependencies..." -ForegroundColor Yellow
} elseif ((Test-Path $requirements) -and ((Get-Item $requirements).LastWriteTime -gt (Get-Item $sentinel).LastWriteTime)) {
    $needsInstall = $true
    Write-Host "  requirements.txt changed - updating dependencies..." -ForegroundColor Yellow
} else {
    Write-Host "  Dependencies up to date (delete .venv\.deps_installed to force reinstall)." -ForegroundColor Gray
}

if ($needsInstall) {
    & $venvPython -m pip install --upgrade pip --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: pip upgrade failed." -ForegroundColor Red
        Read-Host "Press Enter to exit"; exit 1
    }
    & $venvPython -m pip install -r $requirements --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: Dependency install failed. Run:" -ForegroundColor Red
        Write-Host "    .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
        Read-Host "Press Enter to exit"; exit 1
    }
    Set-Content -Path $sentinel -Value "installed"
    Write-Host "  Dependencies installed OK." -ForegroundColor Green
}

$envFile = Join-Path $RootDir ".env"
if (-not (Test-Path $envFile)) {
    $envExample = Join-Path $RootDir ".env.example"
    if (Test-Path $envExample) { Copy-Item $envExample $envFile }
    Write-Host "  NOTE: .env created from template - edit with your API keys." -ForegroundColor Yellow
}

$existingListener = Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existingListener) {
    Write-Host "  Port 3000 is already in use (PID $($existingListener.OwningProcess))." -ForegroundColor Yellow
    Write-Host "  Jarvis may already be running: http://localhost:3000" -ForegroundColor Yellow
    Write-Host "  Run .\stop.ps1 before starting another instance." -ForegroundColor Gray
    Start-Process "http://localhost:3000"
    exit 0
}

Write-Host "  Starting Jarvis at http://localhost:3000 ..." -ForegroundColor Green
Write-Host "  Press Ctrl+C here, or run .\stop.ps1 from another PowerShell window." -ForegroundColor Gray
Write-Host ""

Start-Job -ScriptBlock { Start-Sleep 3; Start-Process "http://localhost:3000" } | Out-Null
& $venvPython main.py

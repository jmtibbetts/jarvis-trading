# Jarvis Trading AI v6.1 — PowerShell Launcher
param([string]$RootDir = $PSScriptRoot)
if (-not $RootDir) { $RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $RootDir

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "   Jarvis Trading AI v6.1  [Windows PowerShell]" -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host ""

$venv = Join-Path $RootDir ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "  .venv not found — running setup first..." -ForegroundColor Yellow
    & powershell.exe -ExecutionPolicy Bypass -File (Join-Path $RootDir "setup.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Setup failed. Run setup.ps1 manually." -ForegroundColor Red
        Read-Host "Press Enter to exit"; exit 1
    }
}

& "$venv\Scripts\Activate.ps1"

$envFile = Join-Path $RootDir ".env"
if (-not (Test-Path $envFile)) {
    $envExample = Join-Path $RootDir ".env.example"
    if (Test-Path $envExample) { Copy-Item $envExample $envFile }
    Write-Host "  NOTE: .env created from template — edit with your API keys." -ForegroundColor Yellow
}

Write-Host "  Starting Jarvis at http://localhost:3000 ..." -ForegroundColor Green
Write-Host "  Press Ctrl+C to stop." -ForegroundColor Gray
Write-Host ""

Start-Job -ScriptBlock { Start-Sleep 3; Start-Process "http://localhost:3000" } | Out-Null
& python main.py

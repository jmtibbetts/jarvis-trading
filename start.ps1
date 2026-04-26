# Jarvis Trading AI v5.0 — Python Edition
Write-Host "`n  Jarvis Trading AI v5.0 (Python Edition)" -ForegroundColor Green
Write-Host "  ==========================================" -ForegroundColor Green

if (-not (Test-Path ".venv")) {
    Write-Host "  Creating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
}

& .\.venv\Scripts\Activate.ps1
pip install -q -r requirements.txt

if (-not (Test-Path "data")) { New-Item -ItemType Directory -Path "data" | Out-Null }

python main.py

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$Port = 3000

function Write-Step {
    param(
        [int]$Number,
        [string]$Message
    )

    Write-Host ""
    Write-Host "[$Number/5] $Message" -ForegroundColor Cyan
}

try {
    Write-Step 1 "Opening the Jarvis project"
    Set-Location -LiteralPath $RootDir
    Write-Host "Project: $RootDir" -ForegroundColor Green

    Write-Step 2 "Backing up configuration and database files"
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupDir = Join-Path $RootDir "backups\update-$stamp"
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

    $envFile = Join-Path $RootDir ".env"
    if (Test-Path -LiteralPath $envFile) {
        Copy-Item -LiteralPath $envFile -Destination (Join-Path $backupDir ".env")
    }

    $dataDir = Join-Path $RootDir "data"
    if (Test-Path -LiteralPath $dataDir) {
        $databaseFiles = Get-ChildItem -LiteralPath $dataDir -File | Where-Object {
            $_.Extension -in ".db", ".sqlite", ".sqlite3" -or
            $_.Name -match "\.(db|sqlite|sqlite3)-(wal|shm)$"
        }

        foreach ($databaseFile in $databaseFiles) {
            Copy-Item -LiteralPath $databaseFile.FullName -Destination $backupDir
        }
    }

    Write-Host "Backup: $backupDir" -ForegroundColor Green

    Write-Step 3 "Checking for an old Jarvis process on port $Port"
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)

    foreach ($listener in $listeners) {
        $processId = [int]$listener.OwningProcess
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue

        if ($null -eq $process) {
            throw "Port $Port is occupied by process $processId, but its details could not be inspected. Stop it manually and run this script again."
        }

        $commandLine = [string]$process.CommandLine
        $isPython = [string]$process.Name -match "^python(w)?\.exe$"
        $isJarvis = $commandLine -like "*$RootDir*" -or $commandLine -match "(^|\s)main\.py(\s|$)"

        if (-not ($isPython -and $isJarvis)) {
            throw "Port $Port belongs to '$($process.Name)' (PID $processId), not this Jarvis installation. Stop or move that application, then run this script again."
        }

        Write-Host "Stopping Jarvis PID $processId..." -ForegroundColor Yellow
        Stop-Process -Id $processId -Force
        Wait-Process -Id $processId -ErrorAction SilentlyContinue
    }

    if ($listeners.Count -eq 0) {
        Write-Host "Port $Port is already free." -ForegroundColor Green
    } else {
        Write-Host "Old Jarvis process stopped." -ForegroundColor Green
    }

    Write-Step 4 "Verifying Python 3.12"
    $pythonVersion = & py -3.12 --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 was not found. Install 64-bit Python 3.12, then run this script again."
    }
    Write-Host $pythonVersion -ForegroundColor Green

    Write-Step 5 "Rebuilding dependencies and starting Jarvis"
    $venvDir = Join-Path $RootDir ".venv"
    if (Test-Path -LiteralPath $venvDir) {
        Write-Host "Removing the old .venv..." -ForegroundColor Yellow
        Remove-Item -LiteralPath $venvDir -Recurse -Force
    }

    & (Join-Path $RootDir "start.bat")
    exit $LASTEXITCODE
}
catch {
    Write-Host ""
    Write-Host "Update stopped: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Your .env and data directory were not deleted." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 1
}

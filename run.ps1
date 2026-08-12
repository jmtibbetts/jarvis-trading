<#
  Jarvis process control.

    .\run.ps1              start (detached) if not already running
    .\run.ps1 -Status      what is running, since when, and whether the
                           code on disk is newer than the running process
    .\run.ps1 -Stop        graceful stop, force only if it refuses
    .\run.ps1 -Restart     stop, wait for the port to release, start
    .\run.ps1 -Foreground  run attached in this window (Ctrl+C to quit)
    .\run.ps1 -Logs        tail the log file

  Detached is the default so -Restart can return control instead of
  blocking the terminal. start.ps1 is left untouched - it still handles
  first-run setup (venv, dependencies, .env) and attached running.
#>
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Status,
    [switch]$Foreground,
    [switch]$Logs,
    [int]$Port = 3000
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$LogFile = Join-Path $Root "data\jarvis.log"
$OutFile = Join-Path $Root "data\server-stdout.log"

function Write-Step($msg, $color = 'Cyan') { Write-Host "  $msg" -ForegroundColor $color }

function Get-JarvisProcess {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    try { return Get-Process -Id $conn.OwningProcess -ErrorAction Stop } catch { return $null }
}

function Wait-PortFree($seconds = 20) {
    for ($i = 0; $i -lt ($seconds * 2); $i++) {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Wait-PortUp($seconds = 60) {
    for ($i = 0; $i -lt ($seconds * 2); $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://localhost:$Port/api/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch {
            # not up yet - the API refuses connections until uvicorn binds
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-Jarvis {
    $proc = Get-JarvisProcess
    if (-not $proc) { Write-Step "Not running (nothing on port $Port)." 'Gray'; return $true }

    Write-Step "Stopping PID $($proc.Id) (up since $($proc.StartTime))..." 'Yellow'
    # Ask first: taskkill without /F lets uvicorn run its shutdown hooks so
    # the scheduler stops cleanly and SQLite checkpoints its WAL.
    & taskkill /PID $proc.Id | Out-Null
    if (Wait-PortFree 10) { Write-Step "Stopped cleanly." 'Green'; return $true }

    Write-Step "Did not exit in 10s - forcing." 'Yellow'
    & taskkill /PID $proc.Id /T /F | Out-Null
    if (Wait-PortFree 10) { Write-Step "Force-stopped." 'Green'; return $true }

    Write-Step "Port $Port is still held. Check for a stuck process." 'Red'
    return $false
}

function Start-Jarvis([bool]$Attached) {
    $existing = Get-JarvisProcess
    if ($existing) {
        Write-Step "Already running (PID $($existing.Id)) at http://localhost:$Port" 'Yellow'
        Write-Step "Use .\run.ps1 -Restart to pick up code changes." 'Gray'
        return
    }
    if (-not (Test-Path $VenvPython)) {
        Write-Step "No virtualenv at $VenvPython - run .\start.ps1 once to set it up." 'Red'
        exit 1
    }

    if ($Attached) {
        Write-Step "Starting attached - Ctrl+C to stop." 'Green'
        & $VenvPython (Join-Path $Root "main.py")
        return
    }

    Write-Step "Starting detached..." 'Green'
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "data") | Out-Null
    Start-Process -FilePath $VenvPython `
        -ArgumentList (Join-Path $Root "main.py") `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutFile `
        -RedirectStandardError (Join-Path $Root "data\server-stderr.log")

    if (Wait-PortUp 60) {
        $p = Get-JarvisProcess
        Write-Step "Up at http://localhost:$Port (PID $($p.Id))" 'Green'
    } else {
        Write-Step "Did not answer /api/health within 60s - check data\server-stderr.log" 'Red'
        exit 1
    }
}

function Show-Status {
    $proc = Get-JarvisProcess
    if (-not $proc) {
        Write-Step "STOPPED - nothing listening on port $Port." 'Red'
        return
    }
    Write-Step "RUNNING  PID $($proc.Id)" 'Green'
    Write-Step "started  $($proc.StartTime)  (up $([math]::Round(((Get-Date) - $proc.StartTime).TotalHours, 1))h)" 'Gray'

    # The question that actually matters: is the running process stale?
    Push-Location $Root
    try {
        # git --since parses a bare timestamp in LOCAL time; converting to UTC
        # first shifted the window forward and hid real commits.
        $since = $proc.StartTime.ToString("yyyy-MM-dd HH:mm:ss")
        $newer = & git log --since="$since" --oneline 2>$null
        if ($newer) {
            Write-Step "STALE - commits landed after this process started:" 'Yellow'
            $newer | ForEach-Object { Write-Host "           $_" -ForegroundColor Yellow }
            Write-Step "Run .\run.ps1 -Restart to load them." 'Yellow'
        } else {
            Write-Step "Code is current - no commits since start." 'Gray'
        }
    } catch {
        # not a git checkout, or git unavailable - status still useful
    } finally { Pop-Location }

    try {
        $health = Invoke-WebRequest -Uri "http://localhost:$Port/api/health" -TimeoutSec 5 -UseBasicParsing
        Write-Step "health   HTTP $($health.StatusCode)" 'Gray'
    } catch {
        Write-Step "health   NOT ANSWERING (process alive but API unresponsive)" 'Red'
    }
}

if ($Logs) {
    if (-not (Test-Path $LogFile)) { Write-Step "No log at $LogFile" 'Red'; exit 1 }
    Write-Step "Tailing $LogFile - Ctrl+C to stop." 'Gray'
    Get-Content $LogFile -Tail 40 -Wait
    exit 0
}
if ($Status)  { Show-Status; exit 0 }
if ($Stop)    { if (Stop-Jarvis) { exit 0 } else { exit 1 } }
if ($Restart) {
    if (-not (Stop-Jarvis)) { exit 1 }
    Start-Sleep -Milliseconds 500
    Start-Jarvis $Foreground.IsPresent
    exit 0
}

Start-Jarvis $Foreground.IsPresent

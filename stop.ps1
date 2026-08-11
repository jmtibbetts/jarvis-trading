# Stop only the Jarvis process listening on its configured local port.
param(
    [int]$Port = 3000,
    [string]$RootDir = $PSScriptRoot
)

if (-not $RootDir) { $RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$resolvedRoot = [System.IO.Path]::GetFullPath($RootDir).TrimEnd('\')

Write-Host "Stopping Jarvis Trading AI on port $Port..." -ForegroundColor Cyan

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if (-not $listeners) {
    Write-Host "Jarvis is not running on port $Port." -ForegroundColor Yellow
    exit 0
}

$stopped = 0
foreach ($processId in ($listeners.OwningProcess | Sort-Object -Unique)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if (-not $process) { continue }

    $commandLine = [string]$process.CommandLine
    $executable = [string]$process.ExecutablePath
    $isJarvis = $commandLine -match '(^|\s|["''])main\.py(["'']|\s|$)' -and (
        $executable.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $commandLine.IndexOf($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    )

    if (-not $isJarvis) {
        Write-Host "Port $Port belongs to another application (PID $processId); nothing was stopped." -ForegroundColor Red
        exit 1
    }

    Stop-Process -Id $processId -Force -ErrorAction Stop
    $stopped++
    Write-Host "Stopped Jarvis (PID $processId)." -ForegroundColor Green
}

if ($stopped -eq 0) {
    Write-Host "No Jarvis process was stopped." -ForegroundColor Yellow
}

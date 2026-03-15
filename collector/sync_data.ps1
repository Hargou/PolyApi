# PolyApi Data Sync — pull from VPS
#
# Usage:
#   .\collector\sync_data.ps1
#   .\collector\sync_data.ps1 -Replay

param(
    [string]$VpsHost = "143.110.129.50",
    [string]$VpsUser = "root",
    [string]$VpsDataDir = "/home/openclaw/polyapi/data_store",
    [switch]$Replay
)

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LocalDataDir = Join-Path $ProjectDir "data_store"

if (-not (Test-Path $LocalDataDir)) {
    New-Item -ItemType Directory -Path $LocalDataDir | Out-Null
}

Write-Host ""
Write-Host "=== PolyApi Data Sync ===" -ForegroundColor Cyan
Write-Host "  VPS:   ${VpsUser}@${VpsHost}:${VpsDataDir}"
Write-Host "  Local: $LocalDataDir"
Write-Host ""
Write-Host "Downloading..." -ForegroundColor Yellow

scp "${VpsUser}@${VpsHost}:${VpsDataDir}/*.jsonl" "$LocalDataDir/"

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: scp failed." -ForegroundColor Red
    exit 1
}

# Summary
$files = Get-ChildItem "$LocalDataDir\*.jsonl" -ErrorAction SilentlyContinue
$fileCount = $files.Count
$totalMB = [math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1MB, 1)

Write-Host ""
Write-Host "=== Sync Complete ===" -ForegroundColor Green
Write-Host "  Files: $fileCount days"
Write-Host "  Size:  ${totalMB}MB"
Write-Host ""

if ($Replay) {
    Write-Host "Running replay..." -ForegroundColor Yellow
    Push-Location $ProjectDir
    python -m collector.replay data_store/ --all
    Pop-Location
}

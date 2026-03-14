# PolyApi Data Sync — incremental pull from VPS
# Only downloads new or updated files (fills in gaps)
#
# Usage:
#   .\collector\sync_data.ps1 -VpsHost 123.45.67.89
#   .\collector\sync_data.ps1 -VpsHost 123.45.67.89 -Replay

param(
    [string]$VpsHost = "YOUR_VPS_IP",
    [string]$VpsUser = "root",
    [string]$VpsDataDir = "/root/polyapi/data_store",
    [switch]$Replay
)

$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LocalDataDir = Join-Path $ProjectDir "data_store"

if (-not (Test-Path $LocalDataDir)) {
    New-Item -ItemType Directory -Path $LocalDataDir | Out-Null
}

Write-Host ""
Write-Host "=== PolyApi Data Sync (incremental) ===" -ForegroundColor Cyan
Write-Host "  VPS:   ${VpsUser}@${VpsHost}:${VpsDataDir}"
Write-Host "  Local: $LocalDataDir"
Write-Host ""

# Get list of files on VPS with sizes
Write-Host "Checking VPS for files..." -ForegroundColor Yellow
$vpsFiles = ssh "${VpsUser}@${VpsHost}" "ls -l ${VpsDataDir}/*.jsonl 2>/dev/null | awk '{print \`$NF, \`$5}'"

if (-not $vpsFiles) {
    Write-Host "No files found on VPS." -ForegroundColor Red
    exit 1
}

# Compare and download only what's missing or smaller locally
$downloaded = 0
$skipped = 0

foreach ($line in $vpsFiles -split "`n") {
    $parts = $line.Trim() -split "\s+"
    if ($parts.Count -lt 2) { continue }

    $remotePath = $parts[0]
    $remoteSize = [long]$parts[1]
    $fileName = Split-Path $remotePath -Leaf
    $localPath = Join-Path $LocalDataDir $fileName

    $needsDownload = $false

    if (-not (Test-Path $localPath)) {
        # File doesn't exist locally
        $needsDownload = $true
        $reason = "new"
    } else {
        $localSize = (Get-Item $localPath).Length
        if ($remoteSize -gt $localSize) {
            # VPS has more data (today's file still being written to)
            $needsDownload = $true
            $reason = "updated (+$([math]::Round(($remoteSize - $localSize) / 1KB, 1))KB)"
        }
    }

    if ($needsDownload) {
        Write-Host "  Downloading $fileName ($reason)..." -ForegroundColor Green
        scp "${VpsUser}@${VpsHost}:${remotePath}" "$localPath"
        $downloaded++
    } else {
        $skipped++
    }
}

# Summary
$files = Get-ChildItem "$LocalDataDir\*.jsonl" -ErrorAction SilentlyContinue
$fileCount = $files.Count
$totalLines = 0
foreach ($f in $files) {
    $totalLines += (Get-Content $f.FullName | Measure-Object -Line).Lines
}
$totalMB = [math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1MB, 1)

Write-Host ""
Write-Host "=== Sync Complete ===" -ForegroundColor Green
Write-Host "  Downloaded: $downloaded files"
Write-Host "  Skipped:    $skipped files (already up to date)"
Write-Host "  Total:      $fileCount days, $totalLines events, ${totalMB}MB"
Write-Host ""

# Optional replay
if ($Replay) {
    Write-Host "Running replay..." -ForegroundColor Yellow
    Push-Location $ProjectDir
    python -m collector.replay data_store/ --all
    Pop-Location
}

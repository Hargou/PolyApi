# PolyApi Data Sync — incremental pull from VPS
# Only downloads new or updated files (compares file sizes)
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
Write-Host "=== PolyApi Data Sync (incremental) ===" -ForegroundColor Cyan
Write-Host "  VPS:   ${VpsUser}@${VpsHost}:${VpsDataDir}"
Write-Host "  Local: $LocalDataDir"
Write-Host ""

# Get remote file list with sizes
Write-Host "Checking VPS for files..." -ForegroundColor Yellow
$remoteList = ssh "${VpsUser}@${VpsHost}" "ls -l ${VpsDataDir}/*.jsonl 2>/dev/null" 2>$null

if (-not $remoteList) {
    Write-Host "No files found on VPS." -ForegroundColor Red
    exit 1
}

$downloaded = 0
$skipped = 0

foreach ($line in $remoteList -split "`n") {
    $parts = $line.Trim() -split '\s+'
    if ($parts.Count -lt 9) { continue }

    $remoteSize = [long]$parts[4]
    $remotePath = $parts[-1]
    $fileName = Split-Path -Leaf $remotePath
    $localPath = Join-Path $LocalDataDir $fileName

    if (-not (Test-Path $localPath)) {
        # New file
        Write-Host "  Downloading $fileName (new)..." -ForegroundColor Green
        scp "${VpsUser}@${VpsHost}:${remotePath}" "$localPath"
        $downloaded++
    } else {
        $localSize = (Get-Item $localPath).Length
        if ($remoteSize -gt $localSize) {
            $diffMB = [math]::Round(($remoteSize - $localSize) / 1MB, 1)
            $skip = $localSize + 1
            Write-Host "  Appending $fileName (+${diffMB}MB, skipping first $([math]::Round($localSize / 1MB))MB)..." -ForegroundColor Yellow
            # Use ssh+tail to download ONLY new bytes (append, not rewrite)
            $fs = [System.IO.File]::OpenWrite($localPath)
            $fs.Seek(0, [System.IO.SeekOrigin]::End) | Out-Null
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = "ssh"
            $psi.Arguments = "${VpsUser}@${VpsHost} `"tail -c +${skip} ${remotePath}`""
            $psi.RedirectStandardOutput = $true
            $psi.UseShellExecute = $false
            $proc = [System.Diagnostics.Process]::Start($psi)
            $proc.StandardOutput.BaseStream.CopyTo($fs)
            $proc.WaitForExit()
            $fs.Close()
            $downloaded++
        } else {
            $skipped++
        }
    }
}

# Summary
$files = Get-ChildItem "$LocalDataDir\*.jsonl" -ErrorAction SilentlyContinue
$fileCount = $files.Count
$totalMB = [math]::Round(($files | Measure-Object -Property Length -Sum).Sum / 1MB, 1)

Write-Host ""
Write-Host "=== Sync Complete ===" -ForegroundColor Green
Write-Host "  Downloaded: $downloaded files"
Write-Host "  Skipped:    $skipped files (already up to date)"
Write-Host "  Total:      $fileCount days, ${totalMB}MB"
Write-Host ""

if ($Replay) {
    Write-Host "Running replay..." -ForegroundColor Yellow
    Push-Location $ProjectDir
    python -m collector.replay data_store/ --all
    Pop-Location
}

# Watch for sync completion, then auto-run preprocess
# Usage: .\collector\watch_and_preprocess.ps1
#
# Monitors 2026-03-15.jsonl file size. When it stops growing for 60s,
# assumes download is complete and runs preprocess.

$dataDir = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "data_store"
$file = Join-Path $dataDir "2026-03-15.jsonl"
$python = "C:\Users\karan\AppData\Local\Programs\Python\Python312-Arm64\python.exe"
$projectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "Watching: $file" -ForegroundColor Cyan
Write-Host "Will auto-preprocess when download completes (stable for 60s)`n"

$lastSize = 0
$stableCount = 0

while ($true) {
    if (-not (Test-Path $file)) {
        Write-Host "File not found, waiting..."
        Start-Sleep 10
        continue
    }

    $currentSize = (Get-Item $file).Length
    $sizeGB = [math]::Round($currentSize / 1GB, 2)

    if ($currentSize -eq $lastSize) {
        $stableCount++
        Write-Host "  $sizeGB GB (stable ${stableCount}/6)" -ForegroundColor Yellow
        if ($stableCount -ge 6) {
            Write-Host "`nDownload complete! ($sizeGB GB)" -ForegroundColor Green
            break
        }
    } else {
        $stableCount = 0
        $speed = [math]::Round(($currentSize - $lastSize) / 1MB / 10, 1)
        Write-Host "  $sizeGB GB (+${speed} MB/s)"
    }

    $lastSize = $currentSize
    Start-Sleep 10
}

# Run preprocess
Write-Host "`n=== Running Preprocess ===" -ForegroundColor Cyan
Write-Host "Input: $dataDir/*.jsonl"
Write-Host "Output: $dataDir/replay_data.parquet`n"

Push-Location $projectDir
& $python -m collector.preprocess data_store/
Pop-Location

Write-Host "`n=== Done! ===" -ForegroundColor Green
Write-Host "Run backtest with:"
Write-Host "  & '$python' test_rust_engine.py"

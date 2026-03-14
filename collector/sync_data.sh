#!/bin/bash
# PolyApi Data Sync — incremental pull from VPS
# Only downloads new or updated files (fills in gaps)
#
# Usage:
#   bash collector/sync_data.sh YOUR_VPS_IP
#   bash collector/sync_data.sh YOUR_VPS_IP --replay

set -e

VPS_HOST="${1:-YOUR_VPS_IP}"
VPS_USER="root"
VPS_DATA_DIR="/root/polyapi/data_store"
REPLAY="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOCAL_DATA_DIR="$PROJECT_DIR/data_store"

mkdir -p "$LOCAL_DATA_DIR"

echo ""
echo "=== PolyApi Data Sync (incremental) ==="
echo "  VPS:   $VPS_USER@$VPS_HOST:$VPS_DATA_DIR"
echo "  Local: $LOCAL_DATA_DIR"
echo ""

# Get remote file list with sizes
echo "Checking VPS for files..."
REMOTE_FILES=$(ssh "$VPS_USER@$VPS_HOST" "ls -l $VPS_DATA_DIR/*.jsonl 2>/dev/null | awk '{print \$NF, \$5}'" || true)

if [ -z "$REMOTE_FILES" ]; then
    echo "No files found on VPS."
    exit 1
fi

DOWNLOADED=0
SKIPPED=0

while IFS= read -r line; do
    REMOTE_PATH=$(echo "$line" | awk '{print $1}')
    REMOTE_SIZE=$(echo "$line" | awk '{print $2}')
    FILENAME=$(basename "$REMOTE_PATH")
    LOCAL_PATH="$LOCAL_DATA_DIR/$FILENAME"

    if [ ! -f "$LOCAL_PATH" ]; then
        # New file
        echo "  Downloading $FILENAME (new)..."
        scp "$VPS_USER@$VPS_HOST:$REMOTE_PATH" "$LOCAL_PATH"
        DOWNLOADED=$((DOWNLOADED + 1))
    else
        LOCAL_SIZE=$(stat -f%z "$LOCAL_PATH" 2>/dev/null || stat -c%s "$LOCAL_PATH" 2>/dev/null || echo 0)
        if [ "$REMOTE_SIZE" -gt "$LOCAL_SIZE" ]; then
            DIFF_KB=$(( (REMOTE_SIZE - LOCAL_SIZE) / 1024 ))
            echo "  Downloading $FILENAME (updated +${DIFF_KB}KB)..."
            scp "$VPS_USER@$VPS_HOST:$REMOTE_PATH" "$LOCAL_PATH"
            DOWNLOADED=$((DOWNLOADED + 1))
        else
            SKIPPED=$((SKIPPED + 1))
        fi
    fi
done <<< "$REMOTE_FILES"

# Summary
FILE_COUNT=$(ls -1 "$LOCAL_DATA_DIR"/*.jsonl 2>/dev/null | wc -l)
TOTAL_LINES=$(cat "$LOCAL_DATA_DIR"/*.jsonl 2>/dev/null | wc -l)
TOTAL_SIZE=$(du -sh "$LOCAL_DATA_DIR" 2>/dev/null | cut -f1)

echo ""
echo "=== Sync Complete ==="
echo "  Downloaded: $DOWNLOADED files"
echo "  Skipped:    $SKIPPED files (already up to date)"
echo "  Total:      $FILE_COUNT days, $TOTAL_LINES events, $TOTAL_SIZE"
echo ""

# Optional replay
if [ "$REPLAY" = "--replay" ]; then
    echo "Running replay..."
    cd "$PROJECT_DIR"
    python -m collector.replay data_store/ --all
fi

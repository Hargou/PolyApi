#!/bin/bash
# PolyApi Data Collector — VPS Setup Script
#
# Run this on your VPS:
#   curl -sSL <this-file> | bash
# Or clone the repo and run:
#   bash collector/setup_vps.sh
#
# Tested on: Ubuntu 22.04+, Debian 12+

set -e

echo "=== PolyApi Data Collector Setup ==="

# 1. Install Python + deps
echo "[1/5] Installing Python..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git

# 2. Clone or update repo
INSTALL_DIR="$HOME/polyapi"
if [ -d "$INSTALL_DIR" ]; then
    echo "[2/5] Updating existing repo..."
    cd "$INSTALL_DIR"
    git pull
else
    echo "[2/5] Cloning repo..."
    git clone https://github.com/Hargou/PolyApi.git "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 3. Create venv and install deps
echo "[3/5] Setting up Python environment..."
python3 -m venv venv
source venv/bin/activate
pip install -q httpx websockets

# 4. Create data directory
echo "[4/5] Creating data directory..."
mkdir -p data_store

# 5. Create systemd service
echo "[5/5] Creating systemd service..."
sudo tee /etc/systemd/system/polyapi-collector.service > /dev/null <<UNIT
[Unit]
Description=PolyApi Data Collector
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m collector.recorder --output $INSTALL_DIR/data_store
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable polyapi-collector
sudo systemctl start polyapi-collector

echo ""
echo "=== Setup Complete ==="
echo ""
echo "  Service: polyapi-collector"
echo "  Data:    $INSTALL_DIR/data_store/"
echo ""
echo "  Commands:"
echo "    sudo systemctl status polyapi-collector   # check status"
echo "    sudo journalctl -u polyapi-collector -f   # view logs"
echo "    sudo systemctl restart polyapi-collector  # restart"
echo "    ls -la $INSTALL_DIR/data_store/           # check data files"
echo ""
echo "  To download data to your local machine:"
echo "    scp your-vps:$INSTALL_DIR/data_store/*.jsonl ./data_store/"
echo ""
echo "  Then replay locally:"
echo "    python -m collector.replay data_store/ --all"
echo ""

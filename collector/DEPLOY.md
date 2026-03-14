# VPS Data Collector — Deployment Guide

## What It Does

Records **all** Polymarket 5-minute crypto market data 24/7:
- **Coinbase spot prices** (BTC, ETH, SOL) via WebSocket
- **CLOB orderbook events** (bids, asks, trades, price changes) via WebSocket
- **Market discovery** (new 5-min windows) via REST every 60s
- **Resolution outcomes** (yes/no) when markets expire

Output: `data_store/YYYY-MM-DD.jsonl` — one file per day, ~50-100MB/day.

---

## Step 1: Deploy to VPS

```bash
ssh root@YOUR_VPS_IP

# Install dependencies
apt-get update && apt-get install -y python3 python3-pip python3-venv git

# Clone repo
git clone https://github.com/Hargou/PolyApi.git ~/polyapi
cd ~/polyapi

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install httpx websockets

# Quick test (Ctrl+C after a few seconds to verify it works)
python -m collector.recorder --output data_store/

# Set up as system service (runs 24/7, survives reboots)
sudo tee /etc/systemd/system/polyapi-collector.service > /dev/null <<EOF
[Unit]
Description=PolyApi Data Collector
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/polyapi
ExecStart=/root/polyapi/venv/bin/python -m collector.recorder --output /root/polyapi/data_store
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable polyapi-collector
sudo systemctl start polyapi-collector
```

---

## Step 2: Verify It's Running

```bash
sudo systemctl status polyapi-collector       # check status
sudo journalctl -u polyapi-collector -f        # live logs
ls -la ~/polyapi/data_store/                   # check files
wc -l ~/polyapi/data_store/*.jsonl             # event counts
```

---

## Step 3: Set Up SSH Key Auth (one-time)

On your **local machine** (Windows):

```powershell
# Generate key if you don't have one
ssh-keygen -t ed25519

# Copy to VPS
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@YOUR_VPS_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"

# Test passwordless login
ssh root@YOUR_VPS_IP "echo ok"
```

---

## Step 4: Pull Data When You Want It

Whenever you want fresh data, run:

```powershell
# From project root — edit VPS_HOST in the script first
.\collector\sync_data.ps1

# Or pass VPS IP directly
.\collector\sync_data.ps1 -VpsHost 123.45.67.89

# Sync + immediately replay all strategies
.\collector\sync_data.ps1 -VpsHost 123.45.67.89 -Replay
```

Or with bash:
```bash
bash collector/sync_data.sh YOUR_VPS_IP
bash collector/sync_data.sh YOUR_VPS_IP --replay
```

Or just plain scp:
```bash
scp root@YOUR_VPS_IP:~/polyapi/data_store/*.jsonl ./data_store/
```

---

## Step 5: Replay & Analyze

```bash
# All strategies comparison
python -m collector.replay data_store/ --all

# Specific strategy
python -m collector.replay data_store/ --strategy quant_models

# Specific day
python -m collector.replay data_store/2026-03-14.jsonl -s fee_extremes

# Custom bankroll
python -m collector.replay data_store/ --all --bankroll 50000
```

---

## VPS Management

```bash
ssh root@YOUR_VPS_IP

# Control service
sudo systemctl status polyapi-collector
sudo systemctl restart polyapi-collector
sudo systemctl stop polyapi-collector

# Logs
sudo journalctl -u polyapi-collector -f
sudo journalctl -u polyapi-collector --since "1 hour ago"

# Update code
cd ~/polyapi && git pull && sudo systemctl restart polyapi-collector

# Disk
du -sh ~/polyapi/data_store/
```

## Resource Usage

- **RAM**: ~50MB | **CPU**: <1% | **Disk**: ~50-100MB/day (~3GB/month) | **Min VPS**: $5/mo

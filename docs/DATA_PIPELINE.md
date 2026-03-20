# Data Pipeline

How raw market data flows from the VPS to backtest results.

## Pipeline Overview

```
VPS (recorder.py)          Local Machine
    │                          │
    │  .\collector\sync_data.ps1   (incremental download)
    │  bash collector/sync_data.sh 143.110.129.50
    v                          v
data_store/                data_store/
├── 2026-03-14.jsonl       ├── 2026-03-14.jsonl  (raw, ~3GB/day)
├── 2026-03-15.jsonl       ├── 2026-03-15.jsonl
│                          │
│                          │  python -m collector.preprocess data_store/
│                          v
│                          ├── replay_data.parquet  (compressed, ~122MB)
│                          │
│                          │  python test_rust_engine.py  (uses ARM64 Python 3.12)
│                          v
│                          └── Results: PnL, win rate, trades per strategy
```

## Step-by-Step

### 1. Record Data (VPS)

```bash
# On VPS — runs 24/7, writes daily JSONL files
python -m collector.recorder --output data_store/
```

Records 5 concurrent streams: Coinbase spot (BTC/ETH/SOL), Chainlink oracle, Polymarket CLOB, market discovery, resolutions.

### 2. Sync to Local

```powershell
# PowerShell (incremental — only downloads new/updated files)
.\collector\sync_data.ps1

# Or bash (also incremental)
bash collector/sync_data.sh 143.110.129.50
```

### 3. Convert JSONL to Parquet

```bash
# Converts all JSONL files in data_store/ to a single replay_data.parquet
python -m collector.preprocess data_store/

# Single file
python -m collector.preprocess data_store/2026-03-14.jsonl

# Both Parquet + filtered JSONL
python -m collector.preprocess data_store/ --format both
```

**Two-pass process:**
- **Pass 1:** Scans `market_info` events to collect known `yes_token_ids`
- **Pass 2:** Filters CLOB events to only known markets, writes Parquet with Snappy compression

**Compression:** Raw ~6GB JSONL → ~122MB Parquet (50x reduction)

### 4. Run Backtest

```bash
# ARM64 Python 3.12 (has poly_engine Rust module installed)
"C:\Users\karan\AppData\Local\Programs\Python\Python312-Arm64\python.exe" test_rust_engine.py

# Or if poly_engine is on PATH
python test_rust_engine.py
```

## Parquet Schema

The Parquet file has a flat schema. Each row is one event. The `type` column determines which fields are populated.

| Column | Type | Used By | Description |
|--------|------|---------|-------------|
| `ts` | int64 | All | Unix timestamp (milliseconds) |
| `type` | string | All | Event type: `spot`, `chainlink`, `clob`, `market_info`, `resolution` |
| **Spot/Chainlink** | | | |
| `sym` | string | spot, chainlink | Symbol: `BTC`, `ETH`, `SOL` |
| `price` | float64 | spot, chainlink | Price in USD |
| **CLOB** | | | |
| `asset_id` | string | clob | Polymarket token ID |
| `condition_id` | string | clob | Market condition ID |
| `event_type` | string | clob | CLOB event: `book`, `trade`, `tick_size_change` |
| `best_bid` | float64 | clob | Best bid price (0-1) |
| `best_ask` | float64 | clob | Best ask price (0-1) |
| `last_trade_price` | float64 | clob | Last trade price |
| `bids_json` | string | clob | JSON array of `[[price, size], ...]` |
| `asks_json` | string | clob | JSON array of `[[price, size], ...]` |
| **Market Info** | | | |
| `mi_condition_id` | string | market_info | Condition ID linking to CLOB events |
| `mi_yes_token_id` | string | market_info | YES token ID for this market |
| `mi_asset` | string | market_info | Asset: `BTC`, `ETH`, `SOL` |
| `mi_slug` | string | market_info | Human-readable market slug |
| `mi_window_start_ts` | int64 | market_info | Window start (unix seconds) |
| `mi_window_end_ts` | int64 | market_info | Window end (unix seconds) |
| `mi_question` | string | market_info | Market question text |
| `mi_volume` | float64 | market_info | Trading volume |
| `mi_liquidity` | float64 | market_info | Liquidity |
| **Resolution** | | | |
| `res_condition_id` | string | resolution | Which market resolved |
| `res_outcome` | string | resolution | Outcome: `yes` or `no` |

## Raw JSONL Format

Each line in the raw JSONL is one event:

```json
{"t": 1710374400000, "type": "spot", "sym": "BTC", "price": 98234.50}
{"t": 1710374401000, "type": "clob", "data": [{"event_type": "book", "asset_id": "abc123", "best_bid": 0.82, "best_ask": 0.84, "bids": [[0.82, 100], [0.80, 200]], "asks": [[0.84, 150]]}]}
{"t": 1710374402000, "type": "market_info", "data": {"condition_id": "xyz", "yes_token_id": "abc123", "asset": "BTC", "slug": "btc-up-5m", "window_start_ts": 1710374400, "window_end_ts": 1710374700}}
{"t": 1710374700000, "type": "resolution", "condition_id": "xyz", "outcome": "yes"}
```

## Data Sizes (Typical)

| File | Size | Events | Notes |
|------|------|--------|-------|
| Raw JSONL (1 day) | ~3 GB | ~9M events | Mostly CLOB book updates |
| Parquet (1 day) | ~122 MB | ~9.7M rows | Snappy compressed |
| Filtered JSONL | ~6 GB | ~18M events | Multiple days, text format |

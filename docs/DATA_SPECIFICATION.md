# PolyApi — Data Specification

**Version:** 1.0  
**Scope:** Event recording, storage, compaction, and run metadata for backtesting and paper trading.

---

## 1. Overview

| Layer | Format | Purpose |
|-------|--------|---------|
| **Live buffer** | JSONL | Fast append during recording |
| **Primary storage** | Parquet | Compressed, queryable, backtest replay |
| **Run metadata** | JSON | Backtest/paper run results |

**Principle:** JSONL for ingestion, Parquet for storage and replay. Compaction runs periodically.

---

## 2. Directory Layout

```
data_store/                    # gitignored
├── events/
│   ├── buffer/                # JSONL staging (live append)
│   │   └── YYYYMMDD_HH.jsonl
│   └── parquet/               # Compressed, partitioned
│       ├── spot/
│       │   └── YYYY-MM-DD.parquet
│       ├── clob/
│       │   └── YYYY-MM-DD.parquet
│       └── markets/
│           └── YYYY-MM-DD.parquet
└── runs/
    └── {run_id}.json
```

---

## 3. Live Buffer (JSONL)

### 3.1 File Naming

`data_store/events/buffer/YYYYMMDD_HH.jsonl`

Example: `20250309_14.jsonl` = events from 2025-03-09 14:00–14:59 UTC.

### 3.2 Line Format

One JSON object per line. No trailing comma. UTF-8.

```json
{"ts": 1710000000.123, "type": "price", "data": {"symbol": "btcusdt", "value": 69000.5}}
{"ts": 1710000000.456, "type": "clob", "data": {"event_type": "best_bid_ask", "asset_id": "0x...", "best_bid": 0.52, "best_ask": 0.54}}
{"ts": 1710000000.789, "type": "clob", "data": {"event_type": "book", "asset_id": "0x...", "bids": [[0.52, 100], [0.51, 200]], "asks": [[0.54, 80], [0.55, 150]]}}
{"ts": 1710000005.0, "type": "markets_update", "data": {"markets": [...]}}
```

### 3.3 Event Types

| type | data fields | Rate |
|------|-------------|------|
| `price` | `symbol`, `value` | ~1/sec per symbol (throttle if needed) |
| `clob` | Passthrough of CLOB payload | See below |
| `markets_update` | `markets` (array) | On discovery refresh (~1/min) |

### 3.4 CLOB Event Subtypes

`data.event_type` determines the payload:

| event_type | Key fields | Rate |
|------------|------------|------|
| `best_bid_ask` | `asset_id`, `best_bid`, `best_ask` | Every update |
| `book` | `asset_id`, `bids`, `asks` | Every 5–10s per asset |
| `last_trade_price` | `asset_id`, `price`, `side`, `size` | Every trade |

### 3.5 Recording Granularity

- **price** — Throttle to max 1/sec per symbol to limit size.
- **best_bid_ask** — Record every CLOB update.
- **book** — Record every 5–10 seconds per asset (needed for L2 fill sim).
- **last_trade_price** — Record every trade.
- **markets_update** — Record when discovery loop refreshes (~60s).

---

## 4. Parquet Schema (Primary Storage)

### 4.1 Spot Prices

**Path:** `data_store/events/parquet/spot/YYYY-MM-DD.parquet`

| Column | Type | Description |
|--------|------|-------------|
| ts | int64 | Unix timestamp nanoseconds |
| symbol | string | btcusdt, ethusdt, solusdt |
| price | float64 | Spot price |

**Compression:** ZSTD (or Snappy for faster writes).

### 4.2 CLOB Events

**Path:** `data_store/events/parquet/clob/YYYY-MM-DD.parquet`

| Column | Type | Description |
|--------|------|-------------|
| ts | int64 | Unix timestamp nanoseconds |
| event_type | string | best_bid_ask, book, last_trade_price |
| asset_id | string | Polymarket token ID |
| best_bid | float64 | Nullable |
| best_ask | float64 | Nullable |
| bids | list<list<float>> | [[price, size], ...] — nullable |
| asks | list<list<float>> | [[price, size], ...] — nullable |
| price | float64 | For last_trade_price — nullable |
| side | string | BUY, SELL — nullable |
| size | float64 | For last_trade_price — nullable |

**Compression:** ZSTD.

### 4.3 Markets Snapshot

**Path:** `data_store/events/parquet/markets/YYYY-MM-DD.parquet`

| Column | Type | Description |
|--------|------|-------------|
| ts | int64 | Unix timestamp nanoseconds |
| markets_json | string | JSON array of market objects |

Markets are stored as JSON for flexibility. Alternative: normalized columns (condition_id, yes_token_id, asset, slug, end_date, etc.) if schema is stable.

**Compression:** ZSTD.

---

## 5. Compaction

### 5.1 Schedule

- **Option A:** Every 5–15 minutes (near real-time).
- **Option B:** Hourly (simpler, slight delay).

### 5.2 Process

1. Read JSONL from `buffer/YYYYMMDD_HH.jsonl`.
2. Parse and split by type: price → spot, clob → clob, markets_update → markets.
3. Append to or create `parquet/{type}/YYYY-MM-DD.parquet`.
4. Optionally delete or archive the JSONL file after successful compaction.

### 5.3 Partitioning

- One Parquet file per day per type.
- For very high volume: partition by `YYYY-MM-DD_HH` (hourly files).

---

## 6. Run Metadata

### 6.1 Path

`data_store/runs/{run_id}.json`

**File naming:** `{run_id}.json` where `run_id` is a UUID (e.g. `a1b2c3d4-e5f6-7890-abcd-ef1234567890.json`).

### 6.2 Schema

```json
{
  "run_id": "uuid",
  "mode": "backtest",
  "strategy": "spot_momentum",
  "strategy_version": "1.0",
  "start_ts": "2025-03-09T14:00:00Z",
  "end_ts": "2025-03-09T15:00:00Z",
  "universe_snapshot": {
    "slugs": ["btc-updown-5m-1710000000", "..."],
    "built_at": "2025-03-09T14:00:00Z"
  },
  "gross_pnl": 125.50,
  "execution_cost_total": 12.30,
  "net_pnl": 113.20,
  "trade_count": 8,
  "blocked_trade_count": 2,
  "sharpe_ratio": null,
  "max_drawdown_pct": null
}
```

### 6.3 Fields

| Field | Type | Description |
|-------|------|-------------|
| run_id | string | UUID |
| mode | string | backtest, paper |
| strategy | string | Strategy name |
| strategy_version | string | Version or hash |
| start_ts | string | ISO8601 |
| end_ts | string | ISO8601 |
| universe_snapshot | object | slugs, built_at |
| gross_pnl | float | Before fees |
| execution_cost_total | float | Slippage + fees |
| net_pnl | float | gross_pnl - execution_cost |
| trade_count | int | Filled trades |
| blocked_trade_count | int | Rejected by risk |
| sharpe_ratio | float | Nullable |
| max_drawdown_pct | float | Nullable |

### 6.4 Per-Trade Log (Optional)

Store in same file or separate `{run_id}_trades.json`:

```json
[
  {"ts": "2025-03-09T14:05:23Z", "side": "buy_yes", "size": 100, "fill_price": 0.52, "slippage_bps": 15, "condition_id": "0x..."},
  ...
]
```

---

## 7. Retention

| Data | Retention |
|------|-----------|
| JSONL buffer | 7 days, then delete or archive |
| Parquet | 30 days (configurable) |
| Run metadata | 90 days (configurable) |

---

## 8. Query Patterns

### 8.1 Backtest Replay

Read Parquet files for date range:

```python
# DuckDB
import duckdb
df = duckdb.query("""
    SELECT * FROM 'data_store/events/parquet/spot/*.parquet'
    WHERE ts BETWEEN 1710000000000000000 AND 1710003600000000000
    ORDER BY ts
""").df()

# Polars
import polars as pl
df = pl.scan_parquet("data_store/events/parquet/spot/*.parquet") \
    .filter(pl.col("ts").is_between(ts_start, ts_end)) \
    .sort("ts") \
    .collect()
```

### 8.2 ReplaySource

`ReplaySource` reads Parquet (or JSONL if Parquet not yet compacted), merges by `ts`, yields events in order.

---

## 9. Timestamp Convention

- **Storage:** Unix nanoseconds (int64) for Parquet. Float seconds for JSONL `ts` (simpler).
- **Consistency:** Use one convention. Prefer int64 nanoseconds for Parquet.

---

## 10. Dependencies

| Package | Purpose |
|---------|---------|
| pyarrow | Parquet read/write |
| duckdb | Optional, for SQL on Parquet |
| polars | Optional, for fast scan |
| aiofiles | Async JSONL append |

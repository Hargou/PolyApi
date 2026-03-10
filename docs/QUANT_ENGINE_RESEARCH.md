# Quant Engine & Data Structures for Prediction Markets — Research Summary

**Purpose:** Research-backed tech and data structures for building quant engines on prediction markets (Polymarket, Kalshi).

---

## 1. Prediction Market Data Model (Core Entities)

### 1.1 Entity Hierarchy

```
Event (e.g. "Will BTC go up in 5 min?")
  └── Market(s) (YES/NO binary contracts)
        └── Orderbook (bid/ask ladders)
        └── Trades (executed history)
        └── Settlement (resolution: 0 or 1)
```

**Sources:** DFlow, Polymarket docs, PredictionData.dev

### 1.2 Binary Outcome Structure

| Concept | Description |
|---------|-------------|
| **Contract** | Settles to exactly $1 (YES) or $0 (NO). Price 1–99¢ = probability proxy. |
| **Zero-sum** | Every dollar of profit = dollar of loss. No drift. |
| **Terminal certainty** | Must eventually settle. Creates expiry-driven strategies. |
| **Polymarket** | Conditional Token Framework (CTF): YES + NO tokens, redeemable for USDC. |
| **Kalshi** | Similar: YES/NO contracts, price 1–99¢. |

### 1.3 Key Data Fields (Per Trade)

| Field | Type | Purpose |
|-------|------|---------|
| execution_price | 1–99 (cents) | Probability at fill |
| taker_side | YES / NO | Who consumed liquidity |
| contract_count | int | Size |
| timestamp | int64 | Ordering |
| outcome | 0 or 1 | Resolution (for PnL) |

**Cost basis:** For YES at 5¢, Cb=5. For NO at 5¢, Cb=5. Normalize by capital risked.

---

## 2. Order Book Data Structures

### 2.1 Polymarket CLOB Structure

| Field | Type | Description |
|-------|------|-------------|
| market | string | Condition ID |
| asset_id | string | Token ID (YES or NO) |
| bids | array | [[price, size], ...] highest first |
| asks | array | [[price, size], ...] lowest first |
| tick_size | string | Min increment (e.g. 0.01) |
| min_order_size | string | Min size |
| hash | string | State hash for change detection |
| timestamp | datetime | Server time |

### 2.2 Order Book Reconstruction (Tick-by-Tick)

**PredictionData.dev / industry pattern:**

1. **Multiple redundant WebSockets** — price_change, book, last_trade
2. **Merge streams** — Reconstruct full book state at each tick
3. **REST sync every 5 min** — Guarantee correctness; correct drift
4. **Per-row = full snapshot** — Each row = complete bid/ask state at that timestamp

**Columns for reconstructed book:**

| Column | Type | Description |
|--------|------|-------------|
| exchange_timestamp | int64 | Exchange time (ms) |
| local_timestamp | int64 | Capture time (ms) |
| bid_prices | string | Comma-separated (high→low) |
| bid_sizes | string | Comma-separated |
| ask_prices | string | Comma-separated (low→high) |
| ask_sizes | string | Comma-separated |

### 2.3 Why Full Book State

- **L2 fill simulation** — Walk levels for realistic slippage
- **Depth metrics** — Imbalance, spread, liquidity
- **Deterministic replay** — Exact state at each tick for backtest

---

## 3. Data Pipeline Architecture (4 Layers)

**Source:** Polymarket Data Pipeline (Medium), industry practice

| Layer | Purpose | Tech |
|-------|---------|------|
| **REST** | Historical data, metadata, current prices | Gamma, CLOB APIs |
| **WebSocket** | Real-time price, order book | CLOB WS |
| **Storage** | Persist in queryable format | Parquet, TSDB |
| **Processing** | Normalize, enrich | ETL, feature compute |

---

## 4. Storage Technology

### 4.1 Format Comparison

| Format | Use Case | Pros | Cons |
|--------|----------|------|------|
| **Parquet** | Primary storage, backtest | 10–20x compression, columnar, DuckDB/Polars | Not append-friendly |
| **JSONL** | Live buffer | Fast append, simple | Verbose, no compression |
| **TimescaleDB** | Live + historical | SQL, 92% compression, append | Heavier than files |
| **QuestDB** | High-throughput tick | 5M+ rows/sec, time-series native | Newer ecosystem |
| **ClickHouse** | Analytics | Columnar, fast | Less ideal for tiny batches |
| **DuckDB** | Query layer | SQL on Parquet, embeddable | Not for ingestion |

### 4.2 Recommendation for PolyApi

| Stage | Format | Reason |
|-------|--------|--------|
| Live ingestion | JSONL or in-memory buffer | Fast append |
| Primary storage | Parquet | Compression, backtest speed |
| Query | DuckDB or Polars | SQL on Parquet, no load |
| Optional | TimescaleDB | If need SQL + append + compression |

### 4.3 Partitioning (DolphinDB / industry)

| Data Type | Partition By | Sort By |
|-----------|--------------|---------|
| Daily OHLC | Year | — |
| 1-min OHLC | Day | — |
| Tick/snapshot | Day + HASH(symbol) | symbol + timestamp |
| L2 tick-by-tick | Hour + factor + HASH(symbol) | symbol + timestamp |

---

## 5. Event-Driven Backtesting

### 5.1 Why Event-Driven (Not Vectorized)

- **No lookahead bias** — Data dripped sequentially
- **Realistic execution** — Partial fills, latency
- **Same code path** — Backtest and live share logic
- **Stateful strategies** — Per-tick state updates

### 5.2 Event Loop Pattern

```
while data_available:
    event = get_next_event()
    if event.type == MARKET_DATA: strategy.on_market(event)
    elif event.type == SIGNAL: order_manager.handle_signal(event)
    elif event.type == ORDER: execution_engine.handle_order(event)
    elif event.type == FILL: portfolio.handle_fill(event)
```

### 5.3 Episode Construction (PredictionMarketBench)

**Standardized inputs for backtest:**

1. **Orderbooks** — Historical L2 snapshots
2. **Trades** — Executed trades with price, side, size
3. **Lifecycle** — Market creation, expiry
4. **Settlement** — Resolution outcome (0 or 1)

**Execution simulator must include:**

- Maker/taker semantics
- Fee modeling
- Deterministic replay

---

## 6. Microstructure Considerations

### 6.1 Maker-Taker Asymmetry (Kalshi Data)

| Role | Avg Excess Return | Notes |
|------|-------------------|-------|
| Taker | -1.12% | Consumes liquidity |
| Maker | +1.12% | Provides liquidity |

**Implication:** Model taker vs maker explicitly. Fee structure favors makers.

### 6.2 Category Efficiency (Kalshi)

| Category | Taker Return | Maker Return | Gap |
|----------|--------------|--------------|-----|
| Finance | -0.08% | +0.08% | 0.17 pp |
| Crypto | -1.34% | +1.34% | 2.69 pp |
| Sports | -1.11% | +1.12% | 2.23 pp |
| Entertainment | -2.40% | +2.40% | 4.79 pp |

**Finance most efficient** — attracts probability-minded traders. **Entertainment/Media** — larger edge for makers.

### 6.3 Longshot Bias

- Low prices (1–20¢) underperform implied probability
- High prices (80–99¢) outperform
- Strongest at tails (1¢ contracts win 0.43% vs 1% implied)

**Data to track:** Price level, taker_side, outcome, category.

---

## 7. Settlement & Resolution Data

### 7.1 Required for PnL

| Field | Description |
|-------|-------------|
| result | YES, NO, void, scalar |
| resolution_ts | When oracle resolved |
| payout | Per contract (100¢ for YES win) |

### 7.2 Polymarket

- Oracle determines outcome
- Winning tokens redeem for USDC
- Conditional Token Framework (CTF)

### 7.3 Cost Basis Tracking

- Position count at settlement
- Cost basis (total paid)
- Revenue = winning_count × 100¢ − cost_basis

---

## 8. Commercial Data Providers (Reference)

| Provider | Offerings | Use Case |
|----------|-----------|----------|
| **PredictionData.dev** | Tick-by-tick books, CSV/Parquet | Polymarket historical |
| **PolymarketData** | L2 books, metrics, 1m resolution | Professional research |
| **PolyTape** | Millisecond books, slippage sim | Execution research |
| **Probalytics** | Polymarket + Kalshi, SSE, ClickHouse | Unified API |

---

## 9. Recommended Data Spec for PolyApi

### 9.1 Event Types to Record

| Type | Granularity | Purpose |
|------|-------------|---------|
| price | 1/sec per symbol | Spot signal |
| best_bid_ask | Every CLOB update | Spread, mid |
| book | Every 5–10s | L2 fill sim |
| last_trade_price | Every trade | Price discovery |
| markets_update | On refresh | Universe |
| settlement | On resolve | PnL attribution |

### 9.2 Redundancy for Correctness

- **Multiple WebSocket streams** if available
- **REST book snapshot every 5 min** — Sync point, correct drift
- **Strict ordering** — Timestamps for deterministic replay

### 9.3 Schema Additions (from Research)

| Addition | Purpose |
|----------|---------|
| exchange_timestamp vs local_timestamp | Latency analysis |
| taker_side on trades | Maker/taker decomposition |
| category / market_type | Cross-sectional analysis |
| outcome (0/1) post-resolution | PnL, calibration |

---

## 10. References

- [PredictionMarketBench (arXiv)](https://arxiv.org/abs/2602.00133) — Episode construction, execution simulator
- [PredictionData.dev Order Books](https://docs.predictiondata.dev/datasets/polymarket/order-books) — Reconstruction methodology
- [DFlow Prediction Market Data Model](https://pond.dflow.net/build/prediction-markets/prediction-market-data-model) — Entity hierarchy
- [Becker: Microstructure of Wealth Transfer](https://www.jbecker.dev/research/prediction-market-microstructure) — Maker/taker, longshot bias
- [Datafield Ch14: Binary Strategies](https://datafield.dev/learning-prediction-markets/part-03/chapter-14/) — Strategy taxonomy, Kelly
- [PBieda: SQLite to Parquet](https://pbieda.com/blog/optimizing-tick-data-storage-from-sqlite-to-parquet) — Storage optimization
- [Shinoji: Tick Data Storage](https://shinoji-research.com/2024/10/12/efficiently-storing-tick-level-financial-data/) — TimescaleDB compression
- [Polymarket CLOB L2 Methods](https://docs.polymarket.com/developers/CLOB/clients/methods-l2) — Order book API

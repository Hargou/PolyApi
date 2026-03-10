# PolyApi — Final Architecture & Data Model

**Version:** 1.0 (Cursor consolidated)  
**Sources:** IMPLEMENTATION_SPEC.md, QUANT_ENGINE_RESEARCH.md, DATA_SPECIFICATION.md  
**Status:** Canonical reference for implementation

---

## 1. Executive Summary

PolyApi is a backtesting and paper-trading platform for Polymarket's 5-minute crypto prediction markets (BTC/ETH/SOL Up/Down). The architecture follows:

- **Unified execution pipeline** — Live and backtest share the same code path
- **Event-driven** — Sequential event processing, no lookahead bias
- **L2-aware execution** — Fill simulation from order book, not midpoint
- **Research-informed** — REST sync for book correctness, maker/taker tracking, Parquet for storage

---

## 2. System Architecture

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         APPLICATION LAYER                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  Recorder │ Backtest Runner │ Paper Trader │ Strategy Lab UI                │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼───────────────────────────────────────┐
│                         STRATEGY RUNTIME                                    │
│  Data Source ──► Signal Layer ──► Risk Engine ──► Order Manager            │
│  (Live/Replay)   (Strategies)      (Limits)         (Fill Sim)               │
│                        │                    │                │              │
│                        └────────────────────┴────────────────┘              │
│                                             ▼                               │
│                                    PORTFOLIO                                │
│                         (Positions, PnL, Settlement)                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼───────────────────────────────────────┐
│                         DATA LAYER                                           │
│  LiveSource │ ReplaySource │ Event Store (JSONL→Parquet) │ Run Store (JSON)  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Module Structure

```
polyapi/
├── app.py
├── engines/                    # Existing: price, market, feed
├── strategies/
│   ├── base.py                 # BaseStrategy, MarketState, Signal
│   ├── registry.py
│   ├── sizing.py               # Kelly
│   └── implementations/
│       ├── spot_momentum.py
│       ├── buy_and_hold.py
│       └── random_baseline.py
├── execution/
│   ├── runner.py               # StrategyRunner
│   ├── context.py              # Clock, StrategyContext
│   ├── portfolio.py
│   ├── order_manager.py
│   ├── risk_engine.py
│   ├── fill_simulator.py
│   └── fees.py
├── data/
│   ├── models.py               # Event, Trade, etc.
│   ├── live_source.py
│   ├── replay_source.py
│   ├── recorder.py
│   ├── event_store.py
│   └── run_store.py
├── analysis/
│   ├── metrics.py
│   └── reporting.py
├── cli/
│   ├── record.py
│   ├── backtest.py
│   └── paper.py
├── config/
│   └── strategies/
└── data_store/                 # gitignored
    ├── events/
    │   ├── buffer/             # JSONL
    │   └── parquet/            # spot, clob, markets
    └── runs/
```

---

## 3. Prediction Market Data Model

### 3.1 Entity Hierarchy (Research-Backed)

```
Event (e.g. "Will BTC go up in 5 min?")
  └── Market(s) (YES/NO binary contracts)
        ├── Orderbook (bid/ask ladders)
        ├── Trades (executed history)
        └── Settlement (resolution: 0 or 1)
```

### 3.2 Binary Outcome Semantics

| Concept | Description |
|---------|-------------|
| Contract | Settles to $1 (YES) or $0 (NO). Price 1–99¢ = probability. |
| Zero-sum | Every $ profit = $ loss. |
| Terminal certainty | Must settle. Enables expiry strategies. |
| Polymarket | CTF: YES + NO tokens, redeemable for USDC. |

### 3.3 Order Book Reconstruction (Industry Pattern)

- **Multiple WebSocket streams** — price_change, book, last_trade
- **REST snapshot every 5 min** — Sync point to correct drift (PredictionData.dev pattern)
- **Per-row = full snapshot** — Complete bid/ask state at each timestamp

---

## 4. Core Data Models

### 4.1 Event (Recording & Replay)

```python
@dataclass(frozen=True)
class Event:
    timestamp_ns: int
    event_type: Literal[
        "spot_price", "clob_book", "clob_best_bid_ask",
        "clob_trade", "market_list", "market_resolution"
    ]
    source: str                    # "coinbase", "polymarket_clob", "gamma"
    asset: Optional[str]           # "BTC", "ETH", "SOL"
    data: dict
    
    # Research addition: dual timestamps for latency analysis
    exchange_timestamp_ns: Optional[int] = None  # When exchange sent
```

### 4.2 MarketState (Strategy Input)

```python
@dataclass(frozen=True)
class MarketState:
    condition_id: str
    yes_token_id: str
    asset: Literal["BTC", "ETH", "SOL"]
    
    best_bid: Decimal
    best_ask: Decimal
    spread_bps: int
    
    spot_price: Decimal
    spot_price_at_window_start: Decimal
    spot_return_bps: int
    
    window_start_ts: int
    window_end_ts: int
    elapsed_sec: int
    remaining_sec: int
    
    bid_depth_5pct: Optional[Decimal] = None
    ask_depth_5pct: Optional[Decimal] = None
    book: Optional["OrderBook"] = None
    bankroll: Optional[Decimal] = None      # From Portfolio, for Kelly sizing
```

### 4.3 Signal (Strategy Output)

```python
@dataclass(frozen=True)
class Signal:
    action: Literal["buy_yes", "buy_no", "sell_yes", "sell_no", "hold"]
    size: int
    size_basis: Literal["fixed", "kelly", "risk_pct"] = "fixed"
    max_slippage_bps: int = 50
    exit_policy: Literal["hold_to_expiry", "trailing_stop", "take_profit"] = "hold_to_expiry"
    
    rationale: str = ""
    p_hat: Optional[Decimal] = None
    ev_bps: Optional[int] = None
```

### 4.4 OrderBook (L2 Fill Sim)

```python
@dataclass
class OrderBook:
    asset_id: str
    timestamp_ns: int
    bids: list[tuple[Decimal, Decimal]]  # [(price, size), ...] best first
    asks: list[tuple[Decimal, Decimal]]
    
    def walk_book(self, side: Literal["buy", "sell"], size: Decimal) -> tuple[Decimal, Decimal]:
        """Returns (avg_fill_price, filled_size)."""
```

### 4.5 Position & Portfolio

```python
class PositionStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLING = "settling"   # Waiting for resolution
    SETTLED = "settled"

@dataclass
class Position:
    id: str
    condition_id: str
    asset: str
    side: Literal["yes", "no"]
    entry_price: Decimal
    size: int
    entry_ts: int
    expiry_ts: int
    status: PositionStatus
    exit_price: Optional[Decimal] = None
    gross_pnl: Optional[Decimal] = None
    fees_paid: Optional[Decimal] = None
    net_pnl: Optional[Decimal] = None

@dataclass
class Portfolio:
    initial_cash: Decimal
    cash: Decimal
    positions: dict[str, Position]
    equity_curve: list[tuple[int, Decimal]]
```

### 4.6 Trade (Research: taker_side)

```python
@dataclass
class Trade:
    timestamp_ns: int
    condition_id: str
    asset_id: str
    price: Decimal
    size: int
    side: Literal["BUY", "SELL"]
    taker_side: Literal["yes", "no"]  # Maker/taker decomposition
    outcome: Optional[int] = None      # 0 or 1 post-resolution
```

---

## 5. Event Storage & Schema

### 5.1 Flow

```
Live → JSONL (buffer) → Compaction (hourly) → Parquet (primary)
```

### 5.2 JSONL Buffer

**Path:** `data_store/events/buffer/YYYYMMDD_HH.jsonl`

```json
{"ts": 1710000000123000000, "type": "spot_price", "source": "coinbase", "asset": "BTC", "data": {"symbol": "btcusdt", "price": 69000.5}}
{"ts": 1710000000456000000, "type": "clob_best_bid_ask", "source": "polymarket_clob", "data": {"asset_id": "0x...", "best_bid": 0.52, "best_ask": 0.54}}
{"ts": 1710000000789000000, "type": "clob_book", "source": "polymarket_clob", "data": {"asset_id": "0x...", "bids": [[0.52, 100]], "asks": [[0.54, 80]]}}
{"ts": 1710000005000000000, "type": "market_list", "source": "gamma", "data": {"markets": [...]}}
```

### 5.3 Parquet (Primary)

| Dataset | Path | Key Columns |
|---------|------|-------------|
| Spot | `parquet/spot/YYYY-MM-DD.parquet` | ts (int64 ns), symbol, price |
| CLOB | `parquet/clob/YYYY-MM-DD.parquet` | ts, event_type, asset_id, best_bid, best_ask, bids, asks, price, side |
| Markets | `parquet/markets/YYYY-MM-DD.parquet` | ts, markets_json |

**Compression:** ZSTD.

### 5.4 Recording Granularity

| Type | Rate | Purpose |
|------|------|---------|
| spot_price | ~1/sec per symbol | Signal |
| clob_best_bid_ask | Every update | Spread |
| clob_book | Every 5–10s per asset | L2 fill sim |
| clob_trade | Every trade | Price discovery, taker_side |
| market_list | On refresh (~60s) | Universe |
| market_resolution | On resolve | PnL |

### 5.5 REST Sync (Research)

- **REST book snapshot every 5 min** — Correct drift from WebSocket
- Use `GET https://clob.polymarket.com/book?token_id=TOKEN_ID`

---

## 6. Execution Flow (Event-Driven)

```
while events_available:
    event = source.next()
    state = build_market_state(event)
    signal = strategy.evaluate(state, context)
    risk_result = risk_engine.check_signal(signal, state, portfolio)
    if not risk_result.allowed:
        log_blocked(signal, risk_result.reason)
        continue
    fill = fill_simulator.simulate(signal, book, fee_model)
    if fill:
        portfolio.open_position(signal, fill)
    if event.type == "market_resolution":
        portfolio.settle_market(condition_id, outcome)
```

---

## 7. Risk Engine

```python
@dataclass(frozen=True)
class RiskLimits:
    max_position_per_market: int = 1000
    max_total_exposure: int = 3000
    max_drawdown_pct: float = 0.10
    max_trades_per_hour: int = 50
    max_correlated_exposure: int = 2000

@dataclass
class RiskCheckResult:
    allowed: bool
    reason: Optional[str] = None
    adjusted_size: Optional[int] = None   # Risk can suggest smaller size

class RiskEngine:
    def check_signal(self, signal, state, portfolio, recent_trades) -> RiskCheckResult:
        # Drawdown circuit breaker, position limits, rate limits, liquidity
        # May return adjusted_size when position limit would be exceeded
```

---

## 8. Fee Model

```python
# Polymarket: 0% entry, 2% on positive returns at settlement
# Fee on profit only — NOT on notional (common mistake)
def calculate_exit_fee(gross_pnl: Decimal, fee_pct: Decimal = Decimal("0.02")) -> Decimal:
    return gross_pnl * fee_pct if gross_pnl > 0 else Decimal("0")
```

---

## 9. Strategy Interface

```python
class BaseStrategy(ABC):
    def evaluate(self, state: MarketState, context: StrategyContext) -> Signal: ...
    def save_state(self) -> dict: ...
    def load_state(self, state: dict) -> None: ...
    def on_market_resolved(self, condition_id: str, outcome: bool, payout: Decimal) -> None: ...
    def reset(self) -> None: ...
```

---

## 10. Microstructure (Research-Informed)

### 10.1 Maker-Taker

| Role | Avg Excess Return (Kalshi) |
|------|---------------------------|
| Taker | -1.12% |
| Maker | +1.12% |

**Action:** Record `taker_side` on trades for decomposition.

### 10.2 Longshot Bias

- Low prices (1–20¢) underperform implied probability
- Track `outcome` (0/1) post-resolution for calibration

### 10.3 Category Efficiency

Finance most efficient; Entertainment/Media less so. Track `category` if available.

---

## 11. Run Metadata

**Path:** `data_store/runs/{run_id}.json`

```json
{
  "run_id": "uuid",
  "mode": "backtest",
  "strategy": "spot_momentum",
  "strategy_version": "1.0",
  "start_ts": "ISO8601",
  "end_ts": "ISO8601",
  "universe_snapshot": {"slugs": [...], "built_at": "..."},
  "gross_pnl": 0,
  "execution_cost_total": 0,
  "net_pnl": 0,
  "trade_count": 0,
  "blocked_trade_count": 0,
  "sharpe_ratio": null,
  "max_drawdown_pct": null
}
```

---

## 12. Implementation Phases (Condensed)

| Phase | Goal | Key Deliverables |
|-------|------|------------------|
| **1** | Data infrastructure | Event model, Recorder, LiveSource adapter, CLI record, **fix fire-and-forget tasks in app.py** |
| **2** | Strategy runtime | **Clock** (RealTime + Simulated), ReplaySource, StrategyRunner, Portfolio, FillSimulator, RiskEngine |
| **3** | Strategies | SpotMomentum, BuyAndHold, Random baseline, Registry |
| **4** | Analysis | Metrics (Sharpe, drawdown), reporting |
| **5** | CLI & UI | backtest, paper CLIs, /api/backtest/*, Strategy Lab |

### 12.1 Clock Abstraction (Testability)

```python
class Clock(ABC):
    def now_ns(self) -> int: ...

class RealTimeClock(Clock): ...
class SimulatedClock(Clock):   # For backtest — advance manually
    def advance(self, delta_ns: int): ...
```

---

## 13. Data Retention & Query

### 13.1 Retention

| Data | Retention |
|------|-----------|
| JSONL buffer | 7 days |
| Parquet | 30 days |
| Run metadata | 90 days |

### 13.2 Query (DuckDB / Polars)

```python
# DuckDB
df = duckdb.query("""
    SELECT * FROM 'data_store/events/parquet/spot/*.parquet'
    WHERE ts BETWEEN :start AND :end ORDER BY ts
""").df()

# Polars
df = pl.scan_parquet("data_store/events/parquet/spot/*.parquet") \
    .filter(pl.col("ts").is_between(ts_start, ts_end)).sort("ts").collect()
```

---

## 14. Research-Informed Additions Summary

| Addition | Source | Purpose |
|----------|--------|---------|
| exchange_timestamp_ns | PredictionData.dev | Latency analysis |
| taker_side on trades | Becker microstructure | Maker/taker decomposition |
| REST book sync every 5 min | PredictionData.dev | Correct WebSocket drift |
| outcome (0/1) post-resolution | PnL, calibration | |
| Parquet primary storage | PBieda, Shinoji | 10–20x compression, backtest speed |
| Event-driven (not vectorized) | PredictionMarketBench | No lookahead, same code path |
| Episode construction | PredictionMarketBench | Orderbooks + trades + lifecycle + settlement |

---

## 15. References

- IMPLEMENTATION_SPEC.md — Full task breakdown
- QUANT_ENGINE_RESEARCH.md — Research sources
- DATA_SPECIFICATION.md — Storage schemas
- [PredictionMarketBench](https://arxiv.org/abs/2602.00133)
- [PredictionData.dev Order Books](https://docs.predictiondata.dev/datasets/polymarket/order-books)
- [Becker: Microstructure](https://www.jbecker.dev/research/prediction-market-microstructure)

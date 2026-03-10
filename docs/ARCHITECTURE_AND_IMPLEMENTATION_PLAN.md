# PolyApi — Architecture & Implementation Plan

**Version:** 1.0 (draft for merge)  
**Scope:** Backtesting, paper trading, strategy framework for Polymarket 5-min crypto markets

---

## Part 1: Architecture

### 1.1 System Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              DATA LAYER                                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│  LiveSource                    │  ReplaySource                    │  Recorder    │
│  (PriceEngine + MarketEngine)  │  (reads JSONL/Parquet)           │  (writes)    │
└───────────────┬────────────────┴────────────────┬────────────────┴──────────────┘
                │                                  │
                └──────────────┬───────────────────┘
                               │ events (price, clob, markets_update)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              EXECUTION LAYER                                      │
├─────────────────────────────────────────────────────────────────────────────────┤
│  StrategyRunner  │  RiskEngine  │  OrderManager  │  FillSimulator  │  Portfolio │
│  (build state,    │  (check      │  (lifecycle,    │  (L2 walk,       │  (positions,│
│   call strategy)  │   limits)     │   risk)         │   slippage)      │   PnL)     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Principle:** Live and backtest share the same execution path. Only the data source differs.

### 1.2 Three-Layer Stack

| Layer | Purpose | Components |
|-------|---------|------------|
| **Signal** | Prices, features, directional logic | MarketState, BaseStrategy |
| **Tradeability** | Liquidity, spread, depth | MarketState.spread_bps, depth |
| **Execution** | Fill cost, slippage, fees | FillSimulator, FeeModel |

### 1.3 Directory Structure

```
PolyApi/
├── app.py
├── engines/                  # existing
├── strategies/
│   ├── __init__.py
│   ├── base.py               # BaseStrategy, MarketState, Signal
│   ├── spot_momentum.py
│   ├── benchmarks/
│   │   ├── buy_and_hold.py
│   │   ├── always_yes.py
│   │   └── random_entry.py
│   └── ...
├── execution/
│   ├── __init__.py
│   ├── runner.py             # StrategyRunner
│   ├── portfolio.py
│   ├── order_manager.py
│   ├── risk_engine.py
│   ├── fill_simulator.py
│   ├── fee_model.py
│   └── sizing.py             # Kelly, contracts_from_kelly
├── data/
│   ├── __init__.py
│   ├── events.py              # Event types, schema
│   ├── live_source.py
│   ├── replay_source.py
│   └── recorder.py
├── data_store/               # gitignored
│   ├── events/                # JSONL (hourly)
│   └── runs/                 # run metadata JSON
├── cli/
│   ├── __init__.py
│   ├── record.py
│   ├── run_backtest.py
│   └── run_paper.py
└── config/
    └── default.yaml           # optional
```

### 1.4 Core Interfaces

#### MarketState

```python
@dataclass
class MarketState:
    condition_id: str
    yes_token_id: str
    asset: str  # BTC, ETH, SOL
    best_bid: float
    best_ask: float
    spread_bps: float
    spot_price: float
    spot_price_at_window_start: float   # required for return calc
    spot_return_pct: float              # derived
    window_start_ts: int
    elapsed_sec: int
    bids: list[tuple[float, float]]     # (price, size)
    asks: list[tuple[float, float]]
    bankroll: float                     # from Portfolio
```

#### Signal

```python
@dataclass
class Signal:
    action: Literal["buy_yes", "buy_no", "sell_yes", "sell_no", "hold"]
    size: int
    max_slippage_bps: int
    rationale: str
    exit_policy: Literal["hold_to_expiry", "dynamic_exit"] = "hold_to_expiry"
    p_hat: Optional[float] = None
    ev_bps: Optional[float] = None
```

#### RiskEngine

```python
@dataclass
class RiskLimits:
    max_position_per_market: int
    max_total_exposure: float
    max_drawdown_pct: float
    max_trades_per_hour: int

class RiskEngine:
    def check_signal(self, signal: Signal, portfolio: Portfolio) -> tuple[bool, str]:
        """Returns (allowed, reason_if_blocked)."""
```

#### FeeModel

```python
# Polymarket: ~0% entry, ~2% on positive returns at settlement
# Fee on profit only (not notional)
def exit_fee(pnl: float, fee_pct: float = 0.02) -> float:
    return pnl * fee_pct if pnl > 0 else 0.0
```

### 1.5 Event Schema (Recording)

**JSONL (live append):**
```json
{"ts": 1710000000.123, "type": "price", "data": {"symbol": "btcusdt", "value": 69000.5}}
{"ts": 1710000000.456, "type": "clob", "data": {"event_type": "best_bid_ask", "asset_id": "...", "best_bid": 0.52, "best_ask": 0.54}}
{"ts": 1710000000.789, "type": "clob", "data": {"event_type": "book", "asset_id": "...", "bids": [[0.52, 100], ...], "asks": [[0.54, 80], ...]}}
{"ts": 1710000005.0, "type": "markets_update", "data": {"markets": [...]}}
```

**Record granularity:**
- `price` — every tick (throttle to 1/s per symbol if needed)
- `best_bid_ask` — every CLOB update
- `book` — every 5–10s per asset (for fill sim)
- `last_trade_price` — every trade
- `markets_update` — on discovery refresh

**Data flow:** JSONL → hourly compaction → Parquet (optional, for query)

### 1.6 Execution Flow

1. **Data source** emits events in order (by ts).
2. **StrategyRunner** maintains state per market, builds `MarketState` from events.
3. **StrategyRunner** calls `strategy.evaluate(state)`.
4. **RiskEngine** checks `check_signal(signal, portfolio)`.
5. **OrderManager** simulates fill via `FillSimulator.weighted_fill()`.
6. **Portfolio** applies fill, updates positions, PnL.
7. On market resolution: **Portfolio** settles, applies `FeeModel.exit_fee()`.

---

## Part 2: Implementation Plan

### Phase 1: Data Recording

**Goal:** Persist live events for replay.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 1.1 | Define event schema in `data/events.py` | `Event` dataclass, type enum |
| 1.2 | Implement `Recorder` with async write (aiofiles) | `data/recorder.py` |
| 1.3 | Wire Recorder to engine callbacks (price, clob, markets_update) | app.py or recorder startup |
| 1.4 | Hourly file rotation (YYYYMMDD_HH.jsonl) | `data_store/events/` |
| 1.5 | CLI: `python -m cli.record --duration 3600` | `cli/record.py` |
| 1.6 | Fix fire-and-forget tasks in app.py | Task tracking, await on shutdown |

**Dependencies:** None (uses existing engines)  
**Acceptance:** Run recorder for 5 min, verify JSONL has price + clob events.

---

### Phase 2a: Strategy + Execution Core (No RiskEngine)

**Goal:** Base types, Portfolio, FillSimulator, FeeModel.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 2a.1 | `MarketState`, `Signal` in `strategies/base.py` | Dataclasses with all fields |
| 2a.2 | `BaseStrategy` abstract class, `save_state`/`load_state` | `strategies/base.py` |
| 2a.3 | `Portfolio` in `execution/portfolio.py` | Positions, PnL, settlement |
| 2a.4 | `FillSimulator` with `weighted_fill()` | `execution/fill_simulator.py` |
| 2a.5 | `FeeModel` | `execution/fee_model.py` |
| 2a.6 | `sizing.py`: `kelly_fraction()`, `contracts_from_kelly()` | `execution/sizing.py` |

**Dependencies:** None  
**Acceptance:** Unit tests for fill sim, Kelly math.

---

### Phase 2b: RiskEngine + OrderManager

**Goal:** Risk checks, order lifecycle.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 2b.1 | `RiskLimits`, `RiskEngine.check_signal()` | `execution/risk_engine.py` |
| 2b.2 | `OrderManager` | Validates signal, calls RiskEngine, FillSimulator, updates Portfolio |
| 2b.3 | `StrategyRunner` | Consumes events, builds state, calls strategy, routes to OrderManager |

**Dependencies:** Phase 2a  
**Acceptance:** StrategyRunner processes a single event, produces fill.

---

### Phase 3: Replay + Backtest Runner

**Goal:** Replay recorded events, run backtest.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 3.1 | `ReplaySource` reads JSONL, yields events in order | `data/replay_source.py` |
| 3.2 | Wire ReplaySource → StrategyRunner → OrderManager → Portfolio | Single-threaded loop |
| 3.3 | CLI: `python -m cli.run_backtest --strategy spot_momentum --data data_store/events/` | `cli/run_backtest.py` |
| 3.4 | `SpotMomentumStrategy` (minimal) | `strategies/spot_momentum.py` |
| 3.5 | Run metadata: run_id, strategy, net_pnl, trade_count, etc. | Write to `data_store/runs/` |

**Dependencies:** Phase 1, 2b  
**Acceptance:** Backtest on 1h of recorded data, produces run JSON.

---

### Phase 4: Paper Trading

**Goal:** Run strategies against live data.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 4.1 | `LiveSource` adapts PriceEngine + MarketEngine to event interface | `data/live_source.py` |
| 4.2 | CLI: `python -m cli.run_paper --strategy spot_momentum` | `cli/run_paper.py` |
| 4.3 | Optional: background task in app for paper run | Or separate process |
| 4.4 | Strategy Lab UI: show active paper run status | `/paper` page, `/api/paper-status` |

**Dependencies:** Phase 2b, 3  
**Acceptance:** Paper run processes live events, produces fills.

---

### Phase 5: Metrics + Benchmarks

**Goal:** Reporting, benchmarks, reproducibility.

| Task | Description | Deliverable |
|------|-------------|-------------|
| 5.1 | Per-run metrics: gross_pnl, execution_cost, net_pnl, Sharpe, max_dd | In run JSON |
| 5.2 | Benchmark strategies: buy_and_hold, always_yes, random_entry | `strategies/benchmarks/` |
| 5.3 | Strategy Lab: backtest results table | `/api/backtest-runs` |
| 5.4 | Per-trade log in run output | Optional CSV/JSON |

**Dependencies:** Phase 3, 4  
**Acceptance:** Run backtest + benchmarks, compare results.

---

## Part 3: Dependencies & Order

```
Phase 1 (data)     ───┬───► Phase 3 (replay)
                      │
Phase 2a (core)   ───┼───► Phase 2b (risk + OM) ───► Phase 3
                      │
                      └───► Phase 4 (paper) ───► Phase 5 (metrics)
```

**Critical path:** 1 → 2a → 2b → 3; 4 can run in parallel with 3 after 2b.

---

## Part 4: Config & Conventions

| Item | Value |
|------|-------|
| Default bankroll | 10_000 |
| Fee model | 0% entry, 2% on positive returns |
| Kelly fraction | 0.25 (quarter Kelly) |
| Data retention | 7 days JSONL, compact to Parquet after |
| Config | `config/default.yaml` or env vars |

---

## Part 5: Open Questions (for merge)

1. **Parallel backtest:** Run multiple strategies in parallel? Design for stateless strategies.
2. **Parameter optimization:** Grid search in Phase 5 or later?
3. **Compaction:** Automated hourly JSONL → Parquet, or manual?
4. **Paper run persistence:** Store paper run state for resume?

# Backtesting & Paper Trading Architecture

A design for testing multiple strategies with paper trading and backtesting on Polymarket 5-min crypto markets.

---

## Core Principle: Unified Execution Pipeline

**Live trading and historical replay must share the same execution path.** No environment flags, no conditional branches—the same StrategyRunner, OrderManager, and Portfolio logic processes every event in both modes. Swapping the data source is the only difference.

---

## Three-Layer Stack

Most backtests fail because they collapse everything into one layer. Separate concerns:

| Layer | Purpose | Data |
|-------|---------|------|
| **Signal** | Historical prices, derived features, directional logic | Spot prices, market probabilities |
| **Tradeability** | Is this market liquid enough to trade right now? | Spread, depth, volume |
| **Execution** | What does it cost to enter/exit at your size? | L2 order book, fill simulation |

**Critical:** Never assume midpoint fills. A strategy showing +80 bps can drop to +20 bps after realistic fill costs. Model execution from L2 depth.

---

## Proposed Directory Structure

```
PolyApi/
├── app.py                    # existing FastAPI app
├── engines/                  # existing: price, market, feed
├── strategies/               # NEW: strategy modules
│   ├── __init__.py
│   ├── base.py               # BaseStrategy interface
│   ├── spot_momentum.py      # Example: spot price vs market probability
│   └── mean_reversion.py     # Example strategy
├── execution/                # NEW: unified execution engine
│   ├── __init__.py
│   ├── runner.py             # StrategyRunner (live + replay)
│   ├── portfolio.py          # Positions, PnL, settlement
│   ├── order_manager.py      # Order lifecycle, risk limits
│   └── fill_simulator.py     # L2-aware fill simulation
├── data/                     # NEW: data abstraction
│   ├── __init__.py
│   ├── live_source.py        # Wraps existing engines (price + market)
│   ├── replay_source.py      # Replays from recorded events
│   └── recorder.py          # Records live events to Parquet/JSONL
├── data_store/               # NEW: persisted data (gitignored)
│   ├── events/               # Raw CLOB + price events
│   └── runs/                 # Backtest run metadata
└── cli/                      # NEW: CLI for backtest/paper runs
    ├── run_backtest.py
    └── run_paper.py
```

---

## Data Sources

### Live (already have)
- **Spot:** Coinbase WebSocket → `PriceEngine`
- **Markets + CLOB:** Gamma + Polymarket CLOB WebSocket → `MarketEngine`

### Historical (for backtesting)

| Source | Free? | What you get |
|--------|-------|--------------|
| **Polymarket CLOB** `/prices-history` | ✅ Yes | Price history per market (asset_id, startTs, endTs, interval=1m) |
| **Recorded live data** | ✅ Yes | Record your own CLOB + spot events for replay |
| **PolymarketData API** | ❌ Paid | L2 books, metrics, spread history—best for serious research |
| **Crypto spot history** | ✅ Yes | CCXT, Yahoo Finance, or record from Coinbase |

**Recommendation:** Start by **recording live data** as you run the dashboard. Persist raw events (price ticks, CLOB book/trade events) with timestamps. That gives you free historical data for replay. Add Polymarket `/prices-history` for resolved markets you didn't record.

---

## Strategy Interface

```python
# strategies/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

@dataclass
class MarketState:
    """Snapshot of market + spot at a point in time."""
    condition_id: str
    yes_token_id: str
    asset: str  # BTC, ETH, SOL
    best_bid: float
    best_ask: float
    spread_bps: float
    spot_price: float
    window_start_ts: int
    elapsed_sec: int
    # ... depth, volume if available

@dataclass
class Signal:
    action: Literal["buy_yes", "buy_no", "hold"]
    size: int  # contracts
    max_slippage_bps: int
    rationale: str

class BaseStrategy(ABC):
    """All strategies implement this interface."""
    
    @abstractmethod
    def evaluate(self, state: MarketState) -> Signal:
        """Produce a trading signal given current market state."""
        pass
    
    def on_market_resolved(self, condition_id: str, outcome: str, payout: float):
        """Optional: handle resolution for PnL attribution."""
        pass
```

---

## Execution Flow

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Data Source    │────▶│  StrategyRunner  │────▶│  OrderManager   │
│  (Live/Replay)  │     │  - Build state   │     │  - Risk checks   │
└─────────────────┘     │  - Call strategy │     │  - Fill sim      │
                        │  - Emit signals   │     │  - Lifecycle     │
                        └──────────────────┘     └────────┬────────┘
                                                          │
                        ┌──────────────────┐              │
                        │    Portfolio     │◀─────────────┘
                        │  - Positions     │
                        │  - PnL           │
                        │  - Settlement    │
                        └──────────────────┘
```

1. **Data source** emits events (price tick, book update, trade, market list).
2. **StrategyRunner** builds `MarketState` from events, calls `strategy.evaluate(state)`.
3. **OrderManager** validates signal, simulates fill from L2 (or uses midpoint for quick tests), updates Portfolio.
4. **Portfolio** tracks positions, realized PnL, handles market resolution (merge/redeem).

---

## Paper Trading vs Backtesting

| Mode | Data Source | Execution | Use Case |
|------|-------------|------------|----------|
| **Paper** | `LiveSource` (wraps PriceEngine + MarketEngine) | Simulated fills at current bid/ask | Test strategies live without real money |
| **Backtest** | `ReplaySource` (reads recorded events) | Simulated fills from historical book | Validate on past data |

Same `StrategyRunner` + `OrderManager` + `Portfolio` in both modes.

---

## Fill Simulation (L2-Aware)

```python
def weighted_fill(levels: list[tuple[float, float]], target_size: float, side: str) -> tuple[float, float, float]:
    """
    Walk order book levels. Returns (avg_fill_price, filled_size, unfilled).
    levels: [(price, size), ...] sorted best-to-worst.
    """
    remaining = float(target_size)
    filled = notional = 0.0
    for price, size in levels:
        take = min(remaining, float(size))
        notional += take * float(price)
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    avg_fill = notional / filled if filled else None
    return avg_fill, filled, max(0, target_size - filled)
```

For each signal: find nearest book snapshot, walk levels, compute slippage vs reference price. Reject if slippage > `max_slippage_bps`.

---

## Implementation Phases

### Phase 1: Data Recording (1–2 days)
- Add `Recorder` that subscribes to engine callbacks, writes events to JSONL/Parquet
- Events: `{ts, type, data}` for price, clob, markets_update
- CLI: `python -m cli.record --duration 3600` (record 1 hour)

### Phase 2: Strategy + Execution Core (2–3 days)
- `BaseStrategy`, `MarketState`, `Signal` dataclasses
- `Portfolio` (positions, PnL, settlement)
- `FillSimulator` (L2 walk, slippage check)
- `OrderManager` (risk limits, lifecycle)

### Phase 3: Replay + Backtest Runner (1–2 days)
- `ReplaySource` reads recorded events, emits in order
- `StrategyRunner` loops over events, builds state, calls strategy
- CLI: `python -m cli.run_backtest --strategy spot_momentum --data data_store/events/2025-03-09.jsonl`

### Phase 4: Paper Trading (1 day)
- `LiveSource` adapts existing engines to same event interface
- Wire into FastAPI: optional background task that runs StrategyRunner with LiveSource
- Or separate process: `python -m cli.run_paper --strategy spot_momentum`

### Phase 5: Metrics + Reporting
- Per-run: gross_pnl, execution_cost, net_pnl, trade_count, blocked_count
- Per-trade: ts, side, size, fill_price, slippage_bps, fill_ratio
- Persist run_id, strategy_version, universe_snapshot for reproducibility

---

## Example Strategy: Spot vs Market Mispricing

For 5-min "BTC Up" markets: if spot is up 0.5% in the window but market probability is only 45%, there may be edge buying Yes.

```python
# strategies/spot_momentum.py
class SpotMomentumStrategy(BaseStrategy):
    def __init__(self, min_edge_bps: int = 50, max_spread_bps: int = 100):
        self.min_edge_bps = min_edge_bps
        self.max_spread_bps = max_spread_bps

    def evaluate(self, state: MarketState) -> Signal:
        if state.spread_bps > self.max_spread_bps:
            return Signal("hold", 0, 0, "spread too wide")
        # Compare spot move vs market probability
        # ... (need window-start spot from state)
        return Signal("buy_yes", 100, 50, "spot up, market underpricing")
```

---

## Run Metadata (Reproducibility)

Every backtest/paper run should log:

```json
{
  "run_id": "uuid",
  "mode": "backtest" | "paper",
  "strategy": "spot_momentum",
  "strategy_version": "1.0",
  "start_ts": "...",
  "end_ts": "...",
  "universe_snapshot": {"slugs": [...], "built_at": "..."},
  "gross_pnl": 0,
  "execution_cost_total": 0,
  "net_pnl": 0,
  "trade_count": 0,
  "blocked_trade_count": 0
}
```

---

## References

- [PolymarketData: How to Backtest Polymarket Strategies](https://polymarketdata.co/blog/how-to-backtest-polymarket-strategies-python) — three-layer stack, L2 fill simulation
- [PolymarketData: OpenClaw + Polymarket](https://polymarketdata.co/blog/openclaw-polymarket-bot-backtesting-guide) — agent output contract, fill simulation
- [Ivan Mijatović: Polymarket Twin Engine](https://ivanmijatovic.com/portfolio/polymarket-trading-bot-backtesting-engine) — unified execution, tick-level replay
- [Polymarket CLOB: prices-history](https://docs.polymarket.com/developers/CLOB/timeseries) — free historical prices API

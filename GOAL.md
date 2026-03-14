# PolyApi — Project Goals

## Vision

**PolyApi** is a platform for researching, backtesting, and paper-trading strategies on Polymarket's 5-minute crypto prediction markets (BTC/ETH/SOL Up/Down). Validate strategies with realistic execution before risking real capital.

---

## Current State (Working)

- **Live dashboard** — Real-time spot prices (Coinbase WS) + Polymarket market data (Gamma + CLOB WS)
- **Single unified feed** — One WebSocket (`/ws/feed`) streams prices, order book, and market updates to the browser
- **Market discovery** — Auto-discovers active 5-min crypto markets every 60s
- **Strategy Lab placeholder** — `/paper` route with CLI instructions

## Not Built Yet

- Strategy framework (BaseStrategy, MarketState, Signal)
- Execution engine (Portfolio, OrderManager, FillSimulator)
- Backtest runner (replay historical data)
- Paper trading (live data through strategies)
- Data fetcher (pull from Polymarket API + PolyBackTest)

---

## Plan

| Phase | What | Key Files to Create |
|-------|------|-------------------|
| **1. Strategy + Execution Core** | BaseStrategy interface, Portfolio, FillSimulator with exact Polymarket fee curve, RiskEngine | `strategies/base.py`, `execution/portfolio.py`, `execution/fill_simulator.py`, `execution/fees.py`, `execution/risk_engine.py` |
| **2. Data Layer** | Fetch historical prices from `/prices-history`, fetch L2 books from PolyBackTest free tier, replay source | `data/fetcher.py`, `data/replay_source.py`, `data/models.py` |
| **3. Backtest Runner** | Event loop: event -> state -> strategy -> signal -> risk -> fill -> portfolio. CLI. | `execution/runner.py`, `cli/backtest.py` |
| **4. First Strategy** | Spot momentum (Bayesian + Kelly). Benchmark strategies (always-yes, random). | `strategies/spot_momentum.py`, `strategies/benchmarks.py` |
| **5. Paper Trading** | Same runner, live data source, live book fetches for fills | `data/live_source.py`, `cli/paper.py` |
| **6. Metrics** | Per-run PnL, Sharpe, drawdown, per-trade logs | `analysis/metrics.py` |

---

## Principles

- **Unified pipeline** — Live and backtest use the same execution path
- **Execution realism** — Model fills from L2 order book, not midpoint
- **Exact fee model** — Polymarket's non-linear taker fee curve (max ~1.56% at 50%)
- **Testable risk** — All risk limits are config params, swept in backtests
- **No VPS needed** — Historical data from Polymarket API + PolyBackTest free tier

---

## Non-Goals

- Live trading with real money (out of scope for now)
- Support for non-5-min crypto markets (focus is narrow)
- Building a custom data recording VPS (free historical data is sufficient)

---

## Key Docs

| Doc | Purpose |
|-----|---------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the live dashboard works |
| [RESEARCH.md](RESEARCH.md) | Fee model, exit policy, risk limits, data sources, paper trading engine design |
| [docs/BACKTEST_PAPER_ARCHITECTURE.md](docs/BACKTEST_PAPER_ARCHITECTURE.md) | Detailed backtest/paper trading architecture |
| [docs/QUANT_ENGINE_RESEARCH.md](docs/QUANT_ENGINE_RESEARCH.md) | Prediction market data structures, microstructure research |

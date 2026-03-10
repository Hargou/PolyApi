# PolyApi — Project Goals

## Vision

**PolyApi** is a platform for researching, backtesting, and paper-trading strategies on Polymarket’s 5-minute crypto prediction markets (BTC/ETH/SOL Up/Down). The goal is to validate strategies with realistic execution before risking real capital.

---

## Current State

- **Live dashboard** — Real-time spot prices (Coinbase) + Polymarket market data (Gamma + CLOB WebSocket)
- **Single unified feed** — One WebSocket (`/ws/feed`) streams prices, order book, and market updates to the browser
- **Pause/Resume** — Frontend can pause the feed to save resources

---

## Ultimate Goal

Enable **multiple strategies** to be tested via:

1. **Paper trading** — Run strategies against live data with simulated fills (no real money)
2. **Backtesting** — Replay historical data through the same execution pipeline for reproducible results

Both modes share the same execution engine. Only the data source changes.

---

## Milestones

| # | Milestone | Description |
|---|-----------|-------------|
| 1 | **Data recording** | Record live price + CLOB events to disk for historical replay |
| 2 | **Strategy framework** | Base strategy interface, market state, signal types |
| 3 | **Execution engine** | Portfolio, order manager, L2-aware fill simulation |
| 4 | **Backtest runner** | Replay recorded data, run strategies, report PnL |
| 5 | **Paper trading** | Run strategies against live feed with simulated execution |
| 6 | **Metrics & reporting** | Per-run and per-trade metrics, reproducibility metadata |

---

## Principles

- **Unified pipeline** — Live and backtest use the same execution path
- **Execution realism** — Model fills from L2 order book, not midpoint
- **Reproducibility** — Pin universe, strategy version, and run metadata for every backtest

---

## Non-Goals

- Live trading with real money (out of scope for now)
- Support for non–5-min crypto markets (focus is narrow)
- Replacement for Polymarket’s official UI (this is a research tool)

---

## See Also

- [docs/BACKTEST_PAPER_ARCHITECTURE.md](docs/BACKTEST_PAPER_ARCHITECTURE.md) — Detailed design for backtesting and paper trading

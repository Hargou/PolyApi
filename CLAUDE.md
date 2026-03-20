# CLAUDE.md — PolyApi Project Instructions

## What This Project Is

Event-driven quant engine for Polymarket's 5-minute crypto binary options (BTC/ETH/SOL Up/Down markets). Backtest strategies against historical data, paper trade against live WebSocket feeds.

## CRITICAL: Data Integrity Issue (2026-03-16)

**Resolution data is unreliable.** Before doing ANY strategy work, read NEXT_STEPS.txt.

- 49% of recorded resolutions disagree with actual spot movement
- `spot_at_window_start` is snapshotted ~20 min early (at market discovery, not window open)
- Recorder uses CLOB `last_trade_price > 0.5` instead of Chainlink (what Polymarket actually uses)
- **All backtest PnL numbers are suspect until this is fixed**
- Verification script: `python scripts/verify_resolutions.py`

## Running Things

```bash
# Backtest (Rust engine, all strategies)
python test_rust_engine.py

# Single strategy backtest
python -m cli.backtest --strategy combo_alpha --markets 10

# Preprocess VPS data to parquet
python -m collector.preprocess data_store/

# Verify resolution accuracy
python scripts/verify_resolutions.py

# Live dashboard
uvicorn app:app --reload
```

## Key Architecture

- **Rust engine** (PyO3 + maturin): reads Parquet, builds MarketState in Rust, calls Python strategy callbacks. `rust_engine/src/replay.rs` is the main loop.
- **Strategies**: all in `strategies/`. Base class in `strategies/base.py`. FADE at price extremes (<0.22 or >0.78) is the only proven edge.
- **Execution pipeline**: `execution/` — fees (non-linear), fill simulator (L2 book walk), risk engine, portfolio.
- **Data**: `data/` — models, fetcher (Polymarket API), replay/live sources.
- **Research docs**: `docs/research/RESEARCH.md` (index), `PARAM_LOG.md` (parameter changes).

## Research Workflow

Use `/research [topic]` or `/research` (auto-picks highest-impact work).

Rules:
- **Never edit existing strategies in-place.** Create new versions (e.g., `combo_alpha_v3.py`).
- **Log every parameter change** to `docs/research/PARAM_LOG.md` with before/after results.
- **Log every backtest run** to `docs/research/backtest_results.md`.
- Read `docs/research/RESEARCH.md` before starting any research to avoid repeating failed experiments.

## Known Constraints

- **Synthetic book data = no OBI signal.** Most CLOB events create 1-level synthetic books. OBI strategies don't work until real VPS L2 data is available.
- **Vol filter is dead code.** Insufficient tick data in backtest -> vol always UNKNOWN.
- **Non-linear fees:** `fee = size * price * 0.25 * (price*(1-price))^2`. Near-zero at extremes, ~1.56% at midpoint. Never trade near 50%.
- **Hold-to-expiry** is the correct default for 5-min markets.
- **Polymarket resolves via Chainlink Data Streams**, not Coinbase. ~0.3 bps divergence matters.

## Toolchain

- Python 3.12 ARM64: `C:\Users\karan\AppData\Local\Programs\Python\Python312-Arm64\python.exe`
- Rust engine build: `cd rust_engine && maturin build --release`

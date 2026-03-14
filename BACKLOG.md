# PolyApi — Implementation Backlog

**How to use:** Tell Claude "resume from backlog.md" in a new session. It will read this file, check what's done, and continue from where it left off.

---

## Status Key
- [ ] Not started
- [x] Done
- [~] In progress

---

## Phase 1: Strategy + Execution Core — DONE

- [x] **1.1** `strategies/__init__.py` — empty init
- [x] **1.2** `strategies/base.py` — BaseStrategy, MarketState, Signal, Position, ExitPolicy
- [x] **1.3** `execution/__init__.py` — empty init
- [x] **1.4** `execution/fees.py` — taker_fee(), maker_rebate(), effective_rate(), round_trip_cost()
- [x] **1.5** `execution/fill_simulator.py` — walk_book(), simulate_fill(), FillResult
- [x] **1.6** `execution/portfolio.py` — Portfolio (open_position, settle, close_position, summary)
- [x] **1.7** `execution/risk_engine.py` — RiskConfig (all params), RiskEngine.check()
- [x] **1.8** `execution/order_manager.py` — OrderManager.process_signal() end-to-end
- [x] **1.9** Integration test: all imports pass, full signal->risk->fill->portfolio flow verified

## Phase 2: Data Layer — DONE

- [x] **2.1** `data/__init__.py` — empty init
- [x] **2.2** `data/models.py` — Event, SpotTick, ClobSnapshot, MarketInfo, MarketResolution
- [x] **2.3** `data/fetcher.py` — fetch_price_history(), fetch_book(), discover_resolved_markets(), fetch_market_by_slug()
- [x] **2.4** `data/replay_source.py` — build_events_from_price_history(), build_replay_stream(), _nearest_spot()

## Phase 3: Backtest Runner — DONE

- [x] **3.1** `execution/runner.py` — StrategyRunner with full event loop, state building, dynamic exit support
- [x] **3.2** `cli/__init__.py` — empty init
- [x] **3.3** `cli/backtest.py` — CLI: `python -m cli.backtest --strategy spot_momentum --all --markets 10`
- [x] **3.4** `analysis/__init__.py` — empty init
- [x] **3.5** `analysis/metrics.py` — compute_metrics(), print_summary() (pnl, fees, win rate, drawdown, profit factor)

## Phase 4: Strategies — DONE

- [x] **4.1** `strategies/spot_momentum.py` — SpotMomentumStrategy with Bayesian logistic, Kelly sizing, configurable params
- [x] **4.2** `strategies/benchmarks.py` — AlwaysYes, AlwaysNo, RandomStrategy
- [x] **4.3** Integration test: synthetic 3-market backtest verified (entries, fills, fees, settlement, PnL all correct)

## Phase 5: Paper Trading — DONE

- [x] **5.1** `data/live_source.py` — LiveSource: chains engine callbacks, translates CLOB WS events (book, best_bid_ask, price_change, last_trade_price), polls L2 books via REST, auto-detects resolutions
- [x] **5.2** `cli/paper.py` — CLI: `python -m cli.paper --strategy spot_momentum --duration 3600 --book-poll 5`. Status line every 30s, trade log + settlement log on exit.
- [x] **5.3** `/paper` page: live PnL dashboard via `/ws/paper` WebSocket, start/stop controls, positions, trade log, settlements, metrics panel. API: `POST /api/paper/start`, `POST /api/paper/stop`, `GET /api/paper/state`

## Phase 6: Metrics & Polish — DONE

- [x] **6.1** `analysis/reporting.py` — generate_run_summary(), save_run_summary(), print_trade_log()
- [x] **6.2** Per-trade log output (ts, side, size, fill_price, slippage, fee, rationale)
- [x] **6.3** `cli/sweep.py` — Parameter sweep CLI: `python -m cli.sweep --param max_drawdown_pct --values 5,10,15,20`
- [x] **6.4** pytest unit tests: test_fees.py (14), test_fill_simulator.py (11), test_portfolio.py (15), test_risk_engine.py (20) — 60 tests, all passing

---

## Cleanup Already Done (2026-03-13)

- [x] Fixed fire-and-forget asyncio tasks in `app.py` (tracked in `_bg_tasks`)
- [x] Deleted 6 stale test files (referenced removed endpoints)
- [x] Deleted 5 redundant doc files (consolidated into RESEARCH.md)
- [x] Added `data_store/` to `.gitignore`
- [x] Removed unused `import time` from `app.py`
- [x] Updated ARCHITECTURE.md project layout
- [x] Created RESEARCH.md (fee model, exit policy, risk limits, data sources, paper trading)
- [x] Updated GOAL.md with clean 6-phase plan

---

## Key Design Decisions (Do Not Re-Research)

**Fee model:** `fee = size * price * 0.25 * (price * (1 - price))^2`. Max ~1.56% at 50%. Takers only. See RESEARCH.md Section 1.

**Exit policy:** Hold-to-expiry default. Dynamic exit via `should_exit()` hook. See RESEARCH.md Section 2.

**Window start price:** Parse from slug: `int(slug.rsplit("-", 1)[1])`. See RESEARCH.md Section 3.

**Risk limits:** All in RiskConfig dataclass. Swept in backtests. See RESEARCH.md Section 4.

**Data:** No VPS. PolyBackTest.com free (L2 books, last 50 5m markets) + Polymarket `/prices-history` (1-min candles). See RESEARCH.md Section 5.

**Paper trading accuracy:** L2 book walk = 10-30 bps error. Good enough. See RESEARCH.md Section 6.

**Book API:** `GET https://clob.polymarket.com/book?token_id={id}` returns `{bids: [{price, size}], asks: [{price, size}], ...}`

**Price history API:** `GET https://clob.polymarket.com/prices-history?market={asset_id}&startTs={unix}&endTs={unix}&fidelity=1` — use explicit timestamps, not `interval=all` (bug for resolved markets).

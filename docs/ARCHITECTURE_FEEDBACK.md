# Architecture Feedback (Curated)

**Review Date:** 2026-03-10  
**Scope:** BACKTEST_PAPER_ARCHITECTURE.md, app.py, engines

Only feedback that is actionable and high-impact. Outdated or low-value items removed.

---

## P0 — Blockers for Phase 2

### 1. Fee Model

Edge detection says "EV > 0 after fees" but fees aren't modeled.

**Polymarket (verify):** ~0% on entry, ~2% on positive returns at settlement.

**Action:** Add `FeeModel` to FillSimulator. A 1.5% gross edge can become negative after fees.

```python
# Fee on profit only, not notional
def exit_fee(pnl: float, fee_pct: float = 0.02) -> float:
    return pnl * fee_pct if pnl > 0 else 0.0
```

### 2. Window Start Price in MarketState

Strategies can't compute spot return without the price at window start.

**Action:** Add to `MarketState`:
- `spot_price_at_window_start: float`
- `spot_return_pct: float` (derived)

### 3. Exit Strategy

Example strategy only emits "buy_yes". Need to specify:
- **Hold to expiry** — position settles automatically (default for 5-min)
- **Dynamic exit** — sell before expiry if edge disappears

**Action:** Document in architecture. Add `exit_policy` to Signal or strategy contract.

### 4. RiskEngine Spec (Before OrderManager)

Risk can't be bolted on later. Design before building OrderManager.

**Action:** Add minimal spec:
- `max_position_per_market`
- `max_total_exposure`
- `max_drawdown_pct` (circuit breaker)
- `check_signal(signal, portfolio) -> (allowed, reason)`

---

## P1 — Important for Quality

### 5. What to Record (Order Book Granularity)

Recording "CLOB events" is vague. For L2 fill sim you need book depth.

**Action:** Phase 1 recorder should capture:
- `best_bid_ask` — spread tracking
- `book` snapshots every 5–10s — fill simulation
- `last_trade_price` — price discovery

### 6. Data Format

**Action:** Use this flow:
```
Live → JSONL (append) → Hourly compact → Parquet (queryable)
```

Define schema for both.

### 7. Fire-and-Forget Tasks (app.py)

`asyncio.create_task(feed.broadcast(...))` with no reference can cause shutdown warnings.

**Action:** Track tasks and await on shutdown:
```python
_tasks: set[asyncio.Task] = set()
def _broadcast(msg):
    t = asyncio.create_task(feed.broadcast(msg))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
# In lifespan cleanup: await asyncio.gather(*_tasks)
```

### 8. Benchmark Strategies

Without baselines, +20 bps is meaningless.

**Action:** Add to architecture:
- Buy-and-hold spot
- Always-Yes (naive)
- Random entry

---

## P2 — Nice to Have

### 9. Order Book State in MarketEngine

Currently forwards events only. For paper trading, strategies may need current book state.

**Action:** Defer. Add `OrderBookState` when paper trading needs it.

### 10. Configuration for Strategy Params

**Action:** Start with env vars or a simple `config.yaml`. Don't over-engineer.

---

## Resolved / Out of Scope

| Item | Status |
|------|--------|
| goal.md | ✅ Exists as GOAL.md |
| Polymarket mechanics docs | Separate doc if needed |
| WebSocket message size limit | Low risk for internal tool; skip |
| sessionStorage for frontend | Nice to have; skip for now |
| Reconnection flap detection | Premature |

---

## Summary

**Before Phase 2:** Fee model, window start price, exit policy, RiskEngine spec.  
**During Phase 1:** Recording granularity, data format, task tracking.  
**Before claiming results:** Benchmark strategies.

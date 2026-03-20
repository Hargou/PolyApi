---
name: research
description: Launch research workflow for testing and optimizing PolyApi trading strategies. Use when investigating strategy parameters, building new strategies, or running backtest experiments. Works with or without arguments.
argument-hint: "[optional: topic, strategy name, or leave blank for auto-pick]"
---

# Research Workflow for PolyApi Quant Engine

You are researching and optimizing trading strategies for Polymarket 5-minute crypto binary options (BTC/ETH/SOL Up/Down markets).

---

## Step 0: Determine What To Do

**If the user gave a specific task:** $ARGUMENTS — do that.

**If no arguments or "generic":** Auto-pick the highest-impact work. Read `docs/research/RESEARCH.md` (leaderboard + planned investigations) and `docs/research/PARAM_LOG.md` (what's been tried). Then pick ONE of these actions, in priority order:

1. **Try a new idea on the best strategy.** Look at the top performer on the leaderboard (currently combo_alpha_v2). Think about what hypothesis could improve it — a new signal, a tighter filter, a different threshold, a timing tweak. Create a NEW version (e.g. `combo_alpha_v3.py`) to test it.

2. **Pick up a planned investigation.** Check the "Planned" table in RESEARCH.md. Pick the highest-priority one that hasn't been started.

3. **Cross-pollinate between strategies.** If one strategy has a feature that works well, try adding it to another strategy as a new version. Example: binary_reversal's spot gate worked — does combo_alpha benefit from a similar gate?

4. **Explore a new hypothesis.** Come up with something novel based on the MarketState fields available (read `strategies/base.py`). Create a new strategy file to test it.

**Always explain your choice:** "I'm picking [X] because [Y]. Here's my hypothesis: [Z]."

---

## Step 1: Read Context First

Before making ANY changes, read:

1. `docs/research/RESEARCH.md` — Master index, strategy leaderboard, planned investigations
2. `docs/research/PARAM_LOG.md` — Every parameter change with before/after (DO NOT repeat failed experiments)
3. Any numbered `docs/research/00X_*.md` docs relevant to your task
4. `strategies/base.py` — MarketState fields, Signal, Position, ExitPolicy
5. The source strategy file(s) you're basing your work on

---

## Step 2: Core Edge (Must Understand)

**FADE at early-window price extremes is the ONLY profitable edge.**

- Contract price hits <0.22 or >0.78 in first ~90 seconds
- Spot price does NOT confirm the direction (not a legitimate move)
- Buy cheap contracts against the extreme (bet on reversion)
- Fees near-zero at extremes: `fee = size * price * 0.25 * (price*(1-price))^2`

| Price | Fee Rate | vs Midpoint |
|-------|----------|-------------|
| 0.15  | 0.06%    | 26x cheaper |
| 0.50  | 1.56%    | baseline    |

**Never** build strategies that trade near midpoint (50%) — fees destroy all edge.

---

## Step 3: NEVER Edit Existing Strategies In-Place

**Golden rule: existing strategy files are immutable once they work.**

When you want to test parameter changes or new logic:

1. **Copy** the source strategy to a new file with a version bump:
   - `combo_alpha.py` → `combo_alpha_v3.py`
   - `early_fade_v2.py` → `early_fade_v3.py`
   - Or a new name if it's a meaningfully different approach: `momentum_fade.py`

2. **In the new file**, document the lineage at the top:
   ```python
   """
   [StrategyName] — [one-line description]

   Based on: [parent strategy] ([parent file])
   Changes from parent:
     - [change 1]: [old] → [new] (hypothesis: [why])
     - [change 2]: [old] → [new] (hypothesis: [why])
   """
   ```

3. **Add to `test_rust_engine.py`** imports and strategy list

4. **Run backtest**, compare against parent strategy

5. **Log results** to PARAM_LOG.md (see Step 4)

This way we always have the original to compare against, and can see exactly what changed and why.

---

## Step 4: Parameter Testing Discipline

**Every experiment MUST be logged.** One hypothesis at a time.

### Workflow:
1. **Baseline**: Run `python test_rust_engine.py`, record parent strategy results
2. **Create new version**: Copy parent → new file, make changes
3. **Test**: Run `python test_rust_engine.py`, record new version results
4. **Compare**: Side-by-side with parent
5. **Log** to `docs/research/PARAM_LOG.md`:

```markdown
### [new_strategy] — [what changed from parent]
Date: YYYY-MM-DD
Based on: [parent_strategy]
Changes: [param] ([old] → [new])
Hypothesis: [why this should help]
Result: parent PnL/trades/win%/PF → new PnL/trades/win%/PF
Verdict: KEEP / ARCHIVE (if worse than parent) / PENDING
```

If the new version is worse: move it to `strategies/archive/` but KEEP the log entry. Failed experiments prevent future waste.

If the new version is better: update RESEARCH.md leaderboard, keep both files (parent stays as reference).

6. **Log to `docs/research/backtest_results.md`** (see Step 4b)

7. **Comment out tested strategies** in `test_rust_engine.py` so they don't rerun on the same data (see Step 4c)

---

## Step 4b: Backtest Results Log

Every backtest run MUST be logged to `docs/research/backtest_results.md`. This is separate from PARAM_LOG.md — it captures the full snapshot of every run.

Format:

```markdown
### Run [N] — [date]
Data: `data_store/replay_data.parquet` ([start date] to [end date], [row count] rows, [market count] markets)

| Strategy | PnL | Trades | Win% | PF | DD | Status |
|----------|-----|--------|------|----|-----|--------|
| ... | ... | ... | ... | ... | ... | ... |

Notes: [any observations, new strategies tested, etc.]
```

If you don't know the data timeframe, query it:
```python
python -c "
import pandas as pd, datetime
df = pd.read_parquet('data_store/replay_data.parquet')
ts = df['timestamp']
print(f'Rows: {len(df)}')
print(f'Start: {datetime.datetime.fromtimestamp(ts.min()/1000, tz=datetime.timezone.utc)}')
print(f'End: {datetime.datetime.fromtimestamp(ts.max()/1000, tz=datetime.timezone.utc)}')
print(f'Markets: {df[\"condition_id\"].nunique() if \"condition_id\" in df.columns else \"?\"}')"
```

---

## Step 4c: Comment Out Tested Strategies

After a strategy has been fully tested on the current dataset, **comment it out** in `test_rust_engine.py` so future runs only test new/changed strategies. This prevents wasting time re-running known results.

```python
# Already tested on current dataset — see backtest_results.md
# EarlyFadeStrategy(),          # +$291, 41t, 53.7%, PF 1.22
# ComboAlphaV2Strategy(),       # +$462, 78t, 53.8%, PF 1.17

# Active research
NewStrategyBeingTested(),
```

When new data arrives (new parquet file), uncomment all strategies to re-validate on the fresh data.

---

## Step 5: Writing Investigation Docs

When you complete a research investigation:

1. Create `docs/research/[NNN]_[topic].md` (next available number)
   - Include: Date, Status, Problem/Hypothesis, What Was Tried, Evidence (backtest numbers), Conclusion
2. Add row to Research Index in `docs/research/RESEARCH.md`
3. Update Strategy Leaderboard if results changed

---

## Known Constraints (Don't Waste Time On These)

- **Synthetic book data = no OBI signal.** Most CLOB events are `price_change` → Rust engine creates 1-level synthetic books. OBI just reflects spread. Blocked until VPS L2 data.
- **Vol filter is dead code.** Insufficient tick data → vol always UNKNOWN. Don't tune vol thresholds.
- **Kelly cap is irrelevant.** All strategies hit max_size. Kelly fraction tuning won't change results.
- **Hold-to-expiry is correct.** 5-min windows too short for mid-window exits after fees/slippage.
- **Don't trade near midpoint.** Fees at 50% are ~1.56%. Only extremes work.
- **Spot gate on combo_alpha FADE = harmful.** combo_alpha_v3 tried binary_reversal's spot gate on FADE mode → lost $170 of profitable trades. combo_alpha's composite signal already provides quality control; FADE trades are profitable even when spot weakly confirms.

---

## Running Backtests

```bash
# All strategies (reads data_store/replay_data.parquet)
python test_rust_engine.py

# Single strategy via CLI
python -m cli.backtest --strategy [name] --markets 10
```

The Rust engine reads `data_store/replay_data.parquet`. Make sure it exists before running.

---

## Available MarketState Fields (for new ideas)

Read `strategies/base.py` for the full list. Key fields:
- `midpoint`, `best_bid`, `best_ask`, `spread_bps` — orderbook
- `spot_price`, `spot_return_bps` — spot since window open
- `elapsed_sec`, `remaining_sec` — timing within 5-min window
- `obi`, `microprice` — microstructure (unreliable with synthetic books)
- `bids`, `asks` — L2 depth levels (usually synthetic 1-level)
- `other_spot_returns` — cross-asset returns (BTC/ETH/SOL)
- `effective_fee_rate` — non-linear fee at current price
- `condition_id`, `asset` — market identity

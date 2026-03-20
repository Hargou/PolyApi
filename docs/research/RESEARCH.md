# Strategy Research Log

Master index of all research investigations. Each entry links to a detailed doc.

## Key Finding

**FADE at early-window price extremes is the only profitable edge.** All strategies that work share this core: when price hits <0.22 or >0.78 in first 90s and spot doesn't confirm the direction, it's likely an overreaction. Buy cheap contracts against the extreme. Fees near-zero at extremes = structural bonus.

---

## Research Index

### Completed

| # | Investigation | Key Finding | Impact |
|---|--------------|-------------|--------|
| 001 | [Fade Edge Discovery](001_fade_edge_discovery.md) | ALL profit from FADE mode, not CONFIRM. Disabling FADE killed all PnL. | Core insight |
| 002 | [Combo Alpha Analysis](002_combo_alpha_analysis.md) | combo_alpha +$460 via extended CONFIRM window (15-270s) + multi-signal. | Best strategy |
| 003 | [Binary Reversal Root Cause](003_binary_reversal_analysis.md) | Missing spot confirmation gate = fading legitimate moves. Vol filter is dead code. | Fix applied |
| 004 | [Performance Regression](004_perf_regression.md) | time_decay unbounded _spot_history caused 16.5x slowdown. | Fixed |
| 005 | [Microstructure Data Gap](005_microstructure_data.md) | Synthetic book data has no meaningful OBI. Needs real L2 from VPS. | Blocked on data |
| 006 | Asset-specific FADE thresholds | Universal 0.22/0.78 is optimal. Per-asset thresholds (BTC 0.24, SOL 0.20) caused $504 regression. | No improvement |

### Planned

| # | Investigation | Hypothesis |
|---|--------------|-----------|
| 007 | Adaptive Kelly | Scale sizing by recent session win rate |
| 008 | Cross-asset momentum in combo_alpha | Add early_fade_v2's cross-asset boost |
| 009 | Spread-based entry timing | Enter when spreads tighten = better fills |
| 010 | OOS validation (2026-03-15 data) | Verify edge holds on unseen data |

---

## Strategy Leaderboard (2026-03-16, replay_data.parquet)

| Strategy | PnL | Trades | Win% | PF | Status |
|----------|-----|--------|------|----|--------|
| combo_alpha_v2 | +$462 | 78 | 53.8% | 1.17 | Production |
| combo_alpha | +$460 | 80 | 52.5% | 1.17 | Production |
| early_fade_v2 | +$291 | 39 | 53.8% | 1.23 | Production |
| early_fade | +$291 | 41 | 53.7% | 1.22 | Production |
| binary_reversal | +$134 | 37 | 51.4% | 1.11 | Production (spot gate fix confirmed) |

## Archived (11 strategies)

See README.md for full list with reasons.
- combo_alpha_v3: spot gate on FADE → lost $170 of profitable trades
- combo_alpha_v4: asset-specific thresholds → $504 regression

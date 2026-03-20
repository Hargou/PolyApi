# Parameter Change Log

Every parameter modification is logged here with before/after results.
If a change doesn't improve, it gets reverted and noted.

---

## Format

```
### [Strategy] — [What changed]
Date: YYYY-MM-DD
Parameter: name (old → new)
Hypothesis: why we think this helps
Result: PnL, trades, win%, PF (before → after)
Verdict: KEEP / REVERTED / PENDING
```

---

## Log

### early_fade — Disable FADE mode
Date: 2026-03-15
Parameter: fade_enabled (True → False)
Hypothesis: CONFIRM alone might be profitable
Result: +$291/41t/53.7% → $0/0t/0% (ALL trades were FADE)
Verdict: REVERTED — FADE is the entire edge

### early_fade — Rename from fee_extremes
Date: 2026-03-15
Parameter: name ("fee_extremes" → "early_fade")
Hypothesis: Name was misleading — the edge is fading, not fee exploitation
Result: No logic change, just rename
Verdict: KEEP

### combo_alpha — Restore FADE mode
Date: 2026-03-15
Parameter: FADE mode (disabled → enabled), extreme thresholds (0.25/0.75 → 0.22/0.78), max_spread_bps (300 → 1000)
Hypothesis: FADE is the profitable edge, CONFIRM-only was losing
Result: 0%win/12t → +$460/80t/52.5%
Verdict: KEEP — massive improvement

### binary_reversal — Vol-adjusted damping → fixed
Date: 2026-03-16
Parameter: damping calculation (min(0.85, 0.65/vol_ratio) → fixed 0.65)
Hypothesis: Vol-adjusted damping might be giving worse p_fair estimates
Result: -$16/70t/50% → -$16/70t/50% (no change — vol always UNKNOWN)
Verdict: KEEP (simpler code) but had no effect

### binary_reversal — Tighten vol regime bounds
Date: 2026-03-15
Parameter: calm_ratio (0.3 → 0.5), storm_ratio (3.0 → 2.5)
Hypothesis: Tighter bounds = more selective vol filtering
Result: No change — vol always classifies as UNKNOWN (insufficient tick data)
Verdict: KEEP but ineffective

### microstructure_fade — Lower OBI threshold
Date: 2026-03-16
Parameter: min_obi_magnitude (0.08 → 0.02)
Hypothesis: Catch weaker OBI signals from synthetic books
Result: 0t → 13t/0%win/-$1033 (trading on noise)
Verdict: REVERTED to 0.08 — synthetic OBI is not a signal

### orderbook_imbalance — Add price extreme gate + loosen filters
Date: 2026-03-16
Parameter: min_book_depth (3 → 1), min_elapsed_sec (60 → 10), min_edge_bps (80 → 30), added extreme_low/high gates
Hypothesis: Filters too restrictive, add proven extreme-price gatekeeper
Result: Still 0 trades — OBI from synthetic books doesn't diverge enough
Verdict: ARCHIVED — fundamental data limitation

### volatility_regime — Earlier timing + extreme gate
Date: 2026-03-16
Parameter: min_elapsed_sec (90 → 15), max_elapsed_sec (270 → 90), min_edge_bps (60 → 30), min_ticks (10 → 5), added extreme gate
Hypothesis: Earlier entry + extreme-price focus might produce trades
Result: Still 0 trades — Brownian model at extremes doesn't produce sufficient edge
Verdict: ARCHIVED

### combo_alpha_v2 — Graduated CONFIRM thresholds
Date: 2026-03-16
Parameter: NEW STRATEGY. late_confirm_signals=3 (vs v1's 2), min_composite_strength=0.08 (vs 0.05), contradiction filter added
Hypothesis: Tighter late-window filters reduce false CONFIRM trades
Result: +$460/80t/52.5% → +$462/78t/53.8% (dropped 2 losers)
Verdict: KEEP — marginal improvement, higher win rate

### binary_reversal — Add spot confirmation gate
Date: 2026-03-16
Parameter: spot_confirm_min_bps (missing → 5.0), min_edge_bps (30 → 40)
Hypothesis: Fading when spot confirms = fading legitimate moves = losers
Result: -$16/70t/50.0%/PF0.99 → +$134/37t/51.4%/PF1.11
Verdict: KEEP — massive improvement, cut 33 losing trades, flipped to profitable

### combo_alpha_v3 — Spot confirmation gate on FADE mode
Date: 2026-03-15
Based on: combo_alpha_v2
Parameter: NEW STRATEGY. Added spot_confirm_min_bps=5.0 to FADE mode (skip fade when spot confirms extreme)
Hypothesis: Same fix that turned binary_reversal from -$16 to +$134 should help combo_alpha's FADE
Result: +$462/78t/53.8%/PF1.17 → +$291/41t/53.7%/PF1.22 (lost 37 trades worth +$170 net)
Verdict: ARCHIVED — spot gate too aggressive for combo_alpha. Unlike binary_reversal (pure fade), combo_alpha's FADE trades are profitable even when spot weakly confirms because the composite signal filter already provides quality control. The 37 removed trades were net winners.

### combo_alpha_v4 — Asset-specific FADE thresholds (Investigation 006)
Date: 2026-03-15
Based on: combo_alpha_v2
Parameter: NEW STRATEGY. Per-asset extreme thresholds: BTC 0.24/0.76, ETH 0.22/0.78, SOL 0.20/0.80
Hypothesis: BTC (low vol) overreacts at less extreme prices, SOL (high vol) needs deeper extremes
Result: +$462/78t/53.8%/PF1.17 → -$42/79t/49.4%/PF0.99 ($504 regression)
Verdict: ARCHIVED — universal 0.22/0.78 is well-calibrated. Relaxing BTC thresholds brought in low-quality trades, tightening SOL filtered out winners. Asset-specific thresholds don't help.

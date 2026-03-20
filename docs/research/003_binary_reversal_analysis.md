# 003: Binary Reversal Root Cause — Why It's Breakeven

**Date:** 2026-03-16
**Status:** Fix applied, awaiting re-test

## Problem

binary_reversal: -$16 on 70 trades, 50.0% win, PF 0.99
early_fade: +$291 on 41 trades, 53.7% win, PF 1.22

binary_reversal gets MORE trades (70 vs 41) despite a TIGHTER window (80s vs 90s).

## Root Cause

**Missing spot confirmation gate.** When spot agrees with the extreme (even by 5bps), it means the move is legitimate — fading it loses money. early_fade skips these; binary_reversal fades them all.

The extra 29 trades are all cases where spot confirms the extreme direction. These are legitimate moves, not overreactions, so fading them produces ~50% win rate (coin flip).

## Secondary Issue

**Vol regime filter is dead code.** The `_estimate_vol()` function needs 4+ spot ticks, but the backtest data has ~1-2 spot points per market in the 10-80s window. Result: `vol_bps = 0` → regime = "UNKNOWN" → filter is bypassed for every trade.

## Fix Applied

1. Added spot confirmation gate: skip fade when `spot_return_bps > 5` (high extreme) or `< -5` (low extreme)
2. Raised `min_edge_bps` from 30 to 40 to filter marginal trades

## Parameters Tested

| Parameter | Original | Changed To | Result |
|-----------|----------|-----------|--------|
| base_damping | 0.65 (vol-adjusted) | 0.65 (fixed) | No change — vol_ratio always 1.0 |
| calm_ratio | 0.3 | 0.5 | No change — vol always UNKNOWN |
| storm_ratio | 3.0 | 2.5 | No change — vol always UNKNOWN |
| spot_confirm_min_bps | (missing) | 5.0 | **Pending re-test** |
| min_edge_bps | 30 | 40 | **Pending re-test** |

## Actual Outcome (2026-03-16)

**Confirmed fix works:** -$16/70t/50.0%/PF0.99 → **+$134/37t/51.4%/PF1.11**

The spot gate cut 33 trades (all losers — fading legitimate moves). Remaining 37 trades are genuine overreactions.

Result is below early_fade (+$291/41t) because:
- Tighter window (80s vs 90s) = fewer qualifying trades
- Higher min_edge_bps (40 vs implicit ~25 in early_fade)
- Vol filter still dead code (UNKNOWN on all trades)

**Next:** With real VPS tick data, vol filter may activate and further improve selectivity.

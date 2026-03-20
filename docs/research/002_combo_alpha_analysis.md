# 002: Combo Alpha Analysis — Why It's the Best Strategy

**Date:** 2026-03-16
**Status:** Complete

## Summary

combo_alpha (+$460, 80 trades) outperforms early_fade (+$291, 41 trades) by 58% because it captures late-window CONFIRM trades that early_fade misses entirely.

## Key Differences

| Feature | early_fade | combo_alpha |
|---------|-----------|-------------|
| Trading window | FADE only (10-90s) | FADE (15-90s) + CONFIRM (15-270s) |
| Signal source | Spot return only | Spot (0.45) + OBI (0.30) + microprice (0.25) |
| CONFIRM threshold | spot > 5bps (too loose) | 2+ of 3 signals agree (selective) |
| Kelly sizing | Uniform 35% | 40% CONFIRM, 20% FADE (asymmetric) |
| Damping | 0.65 | 0.60 (slightly more aggressive) |

## Where the Extra 39 Trades Come From

- ~41 FADE trades (same as early_fade, first 90s)
- ~39 CONFIRM trades (90-270s window, 2+ signals agree)
- CONFIRM trades average +$4.31 per trade (lower than FADE but still positive)

## combo_alpha_v2 Improvement

Graduated CONFIRM thresholds:
- Early window (15-90s): needs 2+ signals (same as v1)
- Late window (90-270s): needs 3/3 signals + contradiction filter

Result: +$462 on 78 trades (53.8% win) — dropped 2 late-window losers.

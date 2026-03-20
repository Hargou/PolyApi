# 001: FADE Edge Discovery

**Date:** 2026-03-15
**Status:** Complete — core insight established

## Summary

ALL profitable trades come from FADE mode (betting against early-window price extremes), not CONFIRM mode (following the extreme direction).

## Experiment

Disabled FADE mode in fee_extremes (now early_fade) to test if CONFIRM alone was profitable.

### Before (FADE enabled)
- early_fade: +$291, 41 trades, 53.7% win, PF 1.22

### After (FADE disabled)
- early_fade: $0, 0 trades — CONFIRM mode produced zero trades

### Re-enabled FADE
- Confirmed: 100% of PnL came from FADE trades

## Why FADE Works

1. Price at extreme (<0.22 or >0.78) in first 90s is often an overreaction (thin liquidity + initial momentum)
2. Fees near-zero at extremes (0.06% at 0.15 vs 1.56% at 0.50) = structural bonus
3. When spot doesn't confirm the direction, mean reversion toward 0.5 is likely
4. Damping formula: `p_fair = 0.5 + 0.65 * (p_market - 0.5)` shifts p_hat toward center

## Why CONFIRM Fails

- `spot_confirm_min_bps = 5.0` threshold is so low that almost any spot move triggers CONFIRM
- CONFIRM buys expensive contracts (same side as extreme) = hard to clear edge/EV bar
- In practice, CONFIRM almost never produces a tradeable signal

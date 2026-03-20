# 004: Performance Regression — 16.5x Slowdown

**Date:** 2026-03-16
**Status:** Fixed

## Problem

Backtest of 9.7M rows took 2567s (43 min) instead of expected ~150s.

Segment timing showed the regression was concentrated:
```
0→2M:   62.9s (normal)
2M→4M:  2220s (35x slower!)
4M→6M:  93s   (normal again)
6M→8M:  102s  (normal)
8M→9.7M: 89s  (normal)
```

## Root Cause

`time_decay.py` had **unbounded** `_spot_history` dictionary — NO trimming:

```python
hist = self._spot_history.setdefault(state.condition_id, [])
hist.append(state.spot_price)  # ← NO CAP, grows forever
```

Each of the ~200 markets accumulated thousands of spot prices. By the 2M-4M segment, Python's garbage collector was thrashing on millions of accumulated floats.

## Fix

Archived `time_decay.py` (also a losing strategy: -$1,070).

## Results

| Configuration | Time | Speedup |
|--------------|------|---------|
| 10 strategies (with time_decay) | 2567s | baseline |
| 8 strategies (without time_decay/quant_models) | 273s | 9.4x |
| 7 strategies (final set) | 155s | 16.5x |

# 005: Microstructure Data Gap — Why OBI Strategies Fail

**Date:** 2026-03-16
**Status:** Blocked on data

## Problem

microstructure_fade, orderbook_imbalance both got 0 trades (or -$1033 with lowered threshold).

## Root Cause

Most CLOB events in the Parquet data are `price_change` type, not `book` type. The Rust engine creates synthetic 1-level books for non-book events:

```rust
// replay.rs line 517-518
let bids = if bb_v > 0.0 { vec![(bb_v, 100.0)] } else { vec![] };
let asks = if ba_v < 1.0 { vec![(ba_v, 100.0)] } else { vec![] };
```

This produces OBI = (bb - ba) / (bb + ba) ≈ -0.025 (just reflects the spread). The signal is pure noise.

## Experiment: Lowered OBI Threshold

Changed microstructure_fade `min_obi_magnitude` from 0.08 to 0.02 to catch synthetic OBI.

**Result:** 13 trades, 0% win rate, -$1,033. Trading on noise = losing money. Reverted.

## Solution Path

The VPS collector records real `book` events with full L2 depth (multiple price levels, varying sizes). When VPS JSONL data is preprocessed to Parquet, the `bids_json`/`asks_json` columns will contain real order books, producing meaningful OBI.

**Blocked until:** VPS data with real book snapshots is available in Parquet.

## Affected Strategies

- microstructure_fade: Needs real OBI > 0.08
- orderbook_imbalance: Needs `len(bids) >= 3` (real depth levels)
- combo_alpha: Uses OBI as 30% signal weight — would improve with real data

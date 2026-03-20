"""
Parameter Sweep for PolyApi Backtest Engine.

Runs the Rust backtest engine (poly_engine.run_replay) with different parameter
combinations for spot_momentum and fee_extremes strategies, batching multiple
variants per run_replay call for efficiency.

Usage:
    python sweep.py
    python sweep.py --bankroll 5000
    python sweep.py --top 30
    python sweep.py --parquet path/to/data.parquet
"""

import argparse
import itertools
import time
from dataclasses import replace
from typing import List, Tuple

import poly_engine

from strategies.spot_momentum import SpotMomentumStrategy, SpotMomentumConfig
from strategies.fee_extremes import FeeExtremesStrategy, FeeExtremesConfig


# ---------------------------------------------------------------------------
# Configuration: parameter grids
# ---------------------------------------------------------------------------

SPOT_MOMENTUM_GRID = {
    "min_edge_bps": [50, 100, 150, 200, 300],
    "logistic_scale": [0.02, 0.04, 0.06],
    "kelly_fraction": [0.25, 0.35, 0.50],
}

FEE_EXTREMES_GRID = {
    "upper_threshold": [0.72, 0.78, 0.85],
    "lower_threshold": [0.15, 0.22, 0.28],
    "min_edge_bps": [20, 40, 80],
    "kelly_fraction": [0.25, 0.35, 0.50],
}

BATCH_SIZE = 10  # target strategies per run_replay call

DEFAULT_PARQUET = "data_store/replay_data.parquet"
DEFAULT_BANKROLL = 10_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_name(prefix: str, params: dict) -> str:
    """Build a descriptive strategy name from parameter dict."""
    parts = [prefix]
    abbreviations = {
        "min_edge_bps": "edge",
        "logistic_scale": "log",
        "kelly_fraction": "kelly",
        "upper_threshold": "upper",
        "lower_threshold": "lower",
    }
    for key, val in params.items():
        short = abbreviations.get(key, key)
        # Format nicely: remove trailing zeros for floats
        if isinstance(val, float):
            if val == int(val):
                parts.append(f"{short}{int(val)}")
            else:
                parts.append(f"{short}{val}")
        else:
            parts.append(f"{short}{val}")
    return "_".join(parts)


def _expand_grid(grid: dict) -> List[dict]:
    """Expand a parameter grid into a list of dicts (cartesian product)."""
    keys = list(grid.keys())
    values = list(grid.values())
    combos = []
    for vals in itertools.product(*values):
        combos.append(dict(zip(keys, vals)))
    return combos


def _make_spot_momentum_callback(config: SpotMomentumConfig):
    """Create a strategy instance and return its evaluate callable."""
    strategy = SpotMomentumStrategy(config)
    return strategy.evaluate


def _make_fee_extremes_callback(config: FeeExtremesConfig):
    """Create a strategy instance and return its evaluate callable."""
    strategy = FeeExtremesStrategy(config)
    return strategy.evaluate


def _batches(items: list, size: int) -> list:
    """Split a list into batches of given size."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _fmt_pnl(val: float) -> str:
    """Format PnL with sign and dollar."""
    if val >= 0:
        return f"+${val:.2f}"
    return f"-${abs(val):.2f}"


def _fmt_time(seconds: float) -> str:
    """Format seconds into human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


# ---------------------------------------------------------------------------
# Build all strategy variants
# ---------------------------------------------------------------------------

def build_spot_momentum_variants(bankroll: float) -> List[Tuple[str, object]]:
    """
    Build (name, evaluate_fn) tuples for all spot_momentum parameter combos.
    """
    base_config = SpotMomentumConfig(bankroll=bankroll)
    combos = _expand_grid(SPOT_MOMENTUM_GRID)
    variants = []
    for params in combos:
        config = replace(base_config, **params)
        name = _make_name("sm", params)
        callback = _make_spot_momentum_callback(config)
        variants.append((name, callback))
    return variants


def build_fee_extremes_variants(bankroll: float) -> List[Tuple[str, object]]:
    """
    Build (name, evaluate_fn) tuples for all fee_extremes parameter combos.
    """
    base_config = FeeExtremesConfig(bankroll=bankroll)
    combos = _expand_grid(FEE_EXTREMES_GRID)
    variants = []
    for params in combos:
        config = replace(base_config, **params)
        name = _make_name("fe", params)
        callback = _make_fee_extremes_callback(config)
        variants.append((name, callback))
    return variants


# ---------------------------------------------------------------------------
# Main sweep logic
# ---------------------------------------------------------------------------

def run_sweep(parquet_path: str, bankroll: float, top_n: int = 20):
    """Run the full parameter sweep and print results."""
    print("=" * 70)
    print("  POLYAPI PARAMETER SWEEP")
    print("=" * 70)
    print(f"  Parquet:  {parquet_path}")
    print(f"  Bankroll: ${bankroll:,.0f}")
    print()

    # Build all variants
    print("Building strategy variants...")
    sm_variants = build_spot_momentum_variants(bankroll)
    fe_variants = build_fee_extremes_variants(bankroll)

    print(f"  spot_momentum: {len(sm_variants)} combos "
          f"({len(SPOT_MOMENTUM_GRID['min_edge_bps'])} x "
          f"{len(SPOT_MOMENTUM_GRID['logistic_scale'])} x "
          f"{len(SPOT_MOMENTUM_GRID['kelly_fraction'])})")
    print(f"  fee_extremes:  {len(fe_variants)} combos "
          f"({len(FEE_EXTREMES_GRID['upper_threshold'])} x "
          f"{len(FEE_EXTREMES_GRID['lower_threshold'])} x "
          f"{len(FEE_EXTREMES_GRID['min_edge_bps'])} x "
          f"{len(FEE_EXTREMES_GRID['kelly_fraction'])})")

    all_variants = sm_variants + fe_variants
    total_variants = len(all_variants)
    print(f"  TOTAL: {total_variants} strategy variants")
    print()

    # Batch them: mix sm and fe into batches for balanced load
    batches = _batches(all_variants, BATCH_SIZE)
    total_batches = len(batches)

    # Estimate time: ~260s for 8 strategies; scale linearly per batch
    est_per_batch = 260.0  # rough estimate in seconds
    est_total = est_per_batch * total_batches
    print(f"  Batches: {total_batches} (target {BATCH_SIZE} strategies/batch)")
    print(f"  Est. time: ~{_fmt_time(est_total)} "
          f"(~{_fmt_time(est_per_batch)}/batch)")
    print()

    # Run batches
    all_results = []
    sweep_start = time.time()

    for batch_idx, batch in enumerate(batches, 1):
        batch_names = [name for name, _ in batch]
        batch_start = time.time()

        elapsed_so_far = batch_start - sweep_start
        if batch_idx > 1:
            avg_per_batch = elapsed_so_far / (batch_idx - 1)
            remaining = avg_per_batch * (total_batches - batch_idx + 1)
            eta_str = f"  ETA: ~{_fmt_time(remaining)}"
        else:
            eta_str = f"  ETA: ~{_fmt_time(est_total)}"

        print(f"--- Batch {batch_idx}/{total_batches} "
              f"({len(batch)} strategies) ---{eta_str}")
        for name in batch_names:
            print(f"    {name}")

        # Call the Rust engine
        try:
            callbacks = [(name, fn) for name, fn in batch]
            results = poly_engine.run_replay(parquet_path, callbacks, bankroll)
        except Exception as e:
            print(f"  ERROR in batch {batch_idx}: {e}")
            print(f"  Skipping batch and continuing...")
            continue

        batch_elapsed = time.time() - batch_start

        # Collect results
        for result in results:
            all_results.append(result)
            pnl_str = _fmt_pnl(result.net_pnl)
            print(f"    -> {result.strategy_name}: "
                  f"PnL={pnl_str}  trades={result.trade_count}  "
                  f"win={result.win_rate:.1f}%")

        print(f"  Batch completed in {_fmt_time(batch_elapsed)}")
        print()

    total_elapsed = time.time() - sweep_start

    # Sort all results by net_pnl descending
    all_results.sort(key=lambda r: r.net_pnl, reverse=True)

    # Print final table
    print()
    print("=" * 100)
    display_count = min(top_n, len(all_results))
    print(f"  TOP {display_count} STRATEGIES BY PNL  "
          f"(out of {len(all_results)} total)")
    print("=" * 100)
    print(f"{'Rank':>4} | {'Strategy Name':<45} | {'PnL':>10} | "
          f"{'Trades':>6} | {'Win%':>6} | {'PF':>6} | {'DD':>10}")
    print(f"{'-'*4:>4} | {'-'*45:<45} | {'-'*10:>10} | "
          f"{'-'*6:>6} | {'-'*6:>6} | {'-'*6:>6} | {'-'*10:>10}")

    for rank, r in enumerate(all_results[:display_count], 1):
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor < 999 else "inf"
        print(f"{rank:>4} | {r.strategy_name:<45} | "
              f"{_fmt_pnl(r.net_pnl):>10} | "
              f"{r.trade_count:>6} | "
              f"{r.win_rate:>5.1f}% | "
              f"{pf_str:>6} | "
              f"${r.max_drawdown:>9.2f}")

    # Print worst performers too (bottom 5)
    if len(all_results) > display_count:
        print(f"\n{'...':>4} | {'...':<45}")
        worst = all_results[-5:]
        for r in worst:
            rank = all_results.index(r) + 1
            pf_str = f"{r.profit_factor:.2f}" if r.profit_factor < 999 else "inf"
            print(f"{rank:>4} | {r.strategy_name:<45} | "
                  f"{_fmt_pnl(r.net_pnl):>10} | "
                  f"{r.trade_count:>6} | "
                  f"{r.win_rate:>5.1f}% | "
                  f"{pf_str:>6} | "
                  f"${r.max_drawdown:>9.2f}")

    print("=" * 100)

    # Summary stats
    pnl_values = [r.net_pnl for r in all_results]
    profitable = sum(1 for p in pnl_values if p > 0)
    print(f"\n  Total strategies tested: {len(all_results)}")
    print(f"  Profitable: {profitable}/{len(all_results)} "
          f"({profitable/len(all_results)*100:.1f}%)" if all_results else "")
    if pnl_values:
        print(f"  Best PnL:  {_fmt_pnl(max(pnl_values))}")
        print(f"  Worst PnL: {_fmt_pnl(min(pnl_values))}")
        print(f"  Median PnL: {_fmt_pnl(sorted(pnl_values)[len(pnl_values)//2])}")
    print(f"  Total sweep time: {_fmt_time(total_elapsed)}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parameter sweep for PolyApi Rust backtest engine"
    )
    parser.add_argument(
        "--parquet", "-p",
        default=DEFAULT_PARQUET,
        help=f"Path to parquet data file (default: {DEFAULT_PARQUET})"
    )
    parser.add_argument(
        "--bankroll", "-b",
        type=float,
        default=DEFAULT_BANKROLL,
        help=f"Starting bankroll (default: {DEFAULT_BANKROLL})"
    )
    parser.add_argument(
        "--top", "-t",
        type=int,
        default=20,
        help="Number of top results to display (default: 20)"
    )
    args = parser.parse_args()
    run_sweep(args.parquet, args.bankroll, args.top)


if __name__ == "__main__":
    main()

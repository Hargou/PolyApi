"""
Parameter Sweep CLI.

Runs the same strategy with multiple risk configs and compares results.

Usage:
    python -m cli.sweep                              # default sweep
    python -m cli.sweep --strategy spot_momentum      # specific strategy
    python -m cli.sweep --markets 20                  # more data
    python -m cli.sweep --param max_drawdown_pct --values 5,10,15,20
"""

import argparse
import asyncio
import copy
import json
import logging
import sys
from itertools import product

from strategies.spot_momentum import SpotMomentumStrategy
from strategies.benchmarks import AlwaysYesStrategy, AlwaysNoStrategy, RandomStrategy
from strategies.quant_models import QuantModelsStrategy
from strategies.fee_extremes import FeeExtremesStrategy
from strategies.time_decay import TimeDecayStrategy
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.liquidity_vacuum import LiquidityVacuumStrategy
from strategies.consensus import ConsensusStrategy
from execution.runner import StrategyRunner
from execution.portfolio import Portfolio
from execution.risk_engine import RiskConfig
from data.fetcher import discover_resolved_markets
from data.models import MarketInfo
from analysis.metrics import compute_metrics
from analysis.reporting import generate_run_summary, save_run_summary
from cli.backtest import fetch_backtest_data

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

STRATEGIES = {
    "quant_models": lambda: QuantModelsStrategy(),
    "fee_extremes": lambda: FeeExtremesStrategy(),
    "time_decay": lambda: TimeDecayStrategy(),
    "orderbook_imbalance": lambda: OrderBookImbalanceStrategy(),
    "volatility_regime": lambda: VolatilityRegimeStrategy(),
    "liquidity_vacuum": lambda: LiquidityVacuumStrategy(),
    "consensus": lambda: ConsensusStrategy(),
    "spot_momentum": lambda: SpotMomentumStrategy(),
    "always_yes": lambda: AlwaysYesStrategy(),
    "always_no": lambda: AlwaysNoStrategy(),
    "random": lambda: RandomStrategy(),
}

# Default sweep grid: key risk params
DEFAULT_SWEEP = {
    "max_drawdown_pct": [5.0, 10.0, 20.0],
    "max_concurrent_positions": [3, 6, 10],
    "max_spread_bps": [300.0, 500.0, 800.0],
}


def build_configs(sweep_grid: dict) -> list[tuple[str, RiskConfig]]:
    """
    Generate all combinations from a sweep grid.

    Args:
        sweep_grid: {param_name: [value1, value2, ...]}

    Returns:
        List of (label, RiskConfig) tuples.
    """
    param_names = list(sweep_grid.keys())
    value_lists = list(sweep_grid.values())

    configs = []
    for combo in product(*value_lists):
        overrides = dict(zip(param_names, combo))
        label = ", ".join(f"{k}={v}" for k, v in overrides.items())
        cfg = RiskConfig(**overrides)
        configs.append((label, cfg))

    return configs


def parse_sweep_grid(param: str, values_str: str) -> dict:
    """Parse a single --param/--values pair into a sweep grid."""
    values = []
    for v in values_str.split(","):
        v = v.strip()
        try:
            values.append(int(v))
        except ValueError:
            try:
                values.append(float(v))
            except ValueError:
                values.append(v)
    return {param: values}


async def run_sweep(
    strategy_name: str,
    market_count: int = 10,
    bankroll: float = 10_000.0,
    sweep_grid: dict = None,
    save: bool = False,
):
    """Run parameter sweep."""
    if strategy_name not in STRATEGIES:
        print(f"Unknown strategy: {strategy_name}")
        print(f"Available: {', '.join(STRATEGIES.keys())}")
        return

    grid = sweep_grid or DEFAULT_SWEEP
    configs = build_configs(grid)

    print(f"\nParameter Sweep: {strategy_name}")
    print(f"Grid: {json.dumps({k: v for k, v in grid.items()}, default=str)}")
    print(f"Combinations: {len(configs)}")
    print()

    # Fetch data once
    print(f"Fetching data ({market_count} windows per asset)...")
    markets = await discover_resolved_markets(count_per_asset=market_count)
    if not markets:
        print("No markets found.")
        return

    events = await fetch_backtest_data(markets)
    if not events:
        print("No events to replay.")
        return

    print(f"Data: {len(events)} events across {len(markets)} markets")
    print()

    # Run each config
    results = []
    for i, (label, risk_cfg) in enumerate(configs, 1):
        strategy = STRATEGIES[strategy_name]()
        portfolio = Portfolio(bankroll=bankroll)
        runner = StrategyRunner(strategy, portfolio, risk_cfg)
        runner.run(events)
        metrics = compute_metrics(portfolio)

        results.append({
            "label": label,
            "risk_config": risk_cfg.__dict__,
            "metrics": metrics,
        })

        status = "+" if metrics["net_pnl"] > 0 else "-" if metrics["net_pnl"] < 0 else "="
        print(f"  [{i}/{len(configs)}] {status} {label}")
        print(f"           PnL=${metrics['net_pnl']:>8.2f}  trades={metrics['trade_count']:<3}  "
              f"win={metrics['win_rate']:>5.1f}%  dd=${metrics['max_drawdown']:>7.2f}  "
              f"pf={metrics['profit_factor']:>5.2f}")

    # Sort by PnL and print comparison
    results.sort(key=lambda r: r["metrics"]["net_pnl"], reverse=True)

    print()
    print("=" * 90)
    print("  SWEEP RESULTS (sorted by PnL)")
    print("=" * 90)
    print(f"  {'#':>3}  {'Net PnL':>10}  {'Trades':>7}  {'Win%':>6}  {'MaxDD':>8}  "
          f"{'PF':>6}  {'Config'}")
    print(f"  {'-'*3}  {'-'*10}  {'-'*7}  {'-'*6}  {'-'*8}  {'-'*6}  {'-'*40}")

    for i, r in enumerate(results, 1):
        m = r["metrics"]
        print(f"  {i:>3}  ${m['net_pnl']:>9.2f}  {m['trade_count']:>7}  "
              f"{m['win_rate']:>5.1f}%  ${m['max_drawdown']:>7.2f}  "
              f"{m['profit_factor']:>5.2f}  {r['label'][:40]}")

    print("=" * 90)

    # Best config
    best = results[0]
    print(f"\n  Best: {best['label']}")
    print(f"  PnL=${best['metrics']['net_pnl']:.2f}, "
          f"Win Rate={best['metrics']['win_rate']:.1f}%, "
          f"Profit Factor={best['metrics']['profit_factor']:.2f}")

    # Save results
    if save:
        import time as _time
        out_path = f"runs/sweep_{strategy_name}_{int(_time.time())}.json"
        from pathlib import Path
        Path("runs").mkdir(exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({
                "strategy": strategy_name,
                "grid": {k: [str(v) for v in vals] for k, vals in grid.items()},
                "results": results,
            }, f, indent=2)
        print(f"\n  Results saved to {out_path}")


def main():
    parser = argparse.ArgumentParser(description="PolyApi Parameter Sweep")
    parser.add_argument("--strategy", "-s", default="spot_momentum",
                        help=f"Strategy ({', '.join(STRATEGIES.keys())})")
    parser.add_argument("--markets", "-m", type=int, default=10,
                        help="Windows per asset (default: 10)")
    parser.add_argument("--bankroll", "-b", type=float, default=10_000.0,
                        help="Starting bankroll (default: 10000)")
    parser.add_argument("--param", "-p", type=str, default=None,
                        help="Single param to sweep (e.g. max_drawdown_pct)")
    parser.add_argument("--values", "-v", type=str, default=None,
                        help="Comma-separated values for --param (e.g. 5,10,15,20)")
    parser.add_argument("--save", action="store_true",
                        help="Save results to runs/ directory")
    args = parser.parse_args()

    if args.param and args.values:
        grid = parse_sweep_grid(args.param, args.values)
    else:
        grid = None

    asyncio.run(run_sweep(args.strategy, args.markets, args.bankroll, grid, args.save))


if __name__ == "__main__":
    main()

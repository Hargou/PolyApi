"""
Backtest CLI.

Usage:
    python -m cli.backtest                           # default: spot_momentum, fetch recent markets
    python -m cli.backtest --strategy always_yes     # run benchmark
    python -m cli.backtest --strategy random          # random baseline
    python -m cli.backtest --markets 10               # scan last 10 windows per asset
    python -m cli.backtest --all                      # run all strategies and compare
"""

import argparse
import asyncio
import json
import logging
import sys

from strategies.spot_momentum import SpotMomentumStrategy
from strategies.benchmarks import AlwaysYesStrategy, AlwaysNoStrategy, RandomStrategy
from strategies.quant_models import QuantModelsStrategy
from strategies.fee_extremes import FeeExtremesStrategy
from strategies.time_decay import TimeDecayStrategy
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.liquidity_vacuum import LiquidityVacuumStrategy
from strategies.consensus import ConsensusStrategy
from strategies.base import BaseStrategy
from execution.runner import StrategyRunner
from execution.portfolio import Portfolio
from execution.risk_engine import RiskConfig
from data.fetcher import discover_resolved_markets, fetch_price_history, fetch_book
from data.replay_source import build_events_from_price_history
from data.models import MarketInfo
from analysis.metrics import compute_metrics, print_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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


async def fetch_backtest_data(markets: list[MarketInfo]) -> list:
    """Fetch price history for each market and build replay events."""
    all_events = []

    for market in markets:
        try:
            history = await fetch_price_history(
                asset_id=market.yes_token_id,
                start_ts=market.window_start_ts,
                end_ts=market.window_end_ts,
                fidelity=1,
            )
        except Exception as e:
            log.warning("Failed to fetch history for %s: %s", market.slug, e)
            continue

        if not history:
            log.warning("No price history for %s", market.slug)
            continue

        # Use the price history to construct synthetic spot prices
        # (spot at window start = first data point maps to "up" or "down")
        # For now, generate synthetic spot from the market probability
        sym = f"{market.asset.lower()}usdt"

        # We need spot prices to determine outcome. Use a synthetic base price.
        base_prices = {"btc": 84000.0, "eth": 3200.0, "sol": 140.0}
        base = base_prices.get(market.asset.lower(), 1000.0)

        # Synthetic spot: derive from market probability movement
        spot_prices = []
        if history:
            first_p = float(history[0]["p"])
            last_p = float(history[-1]["p"])
            # If market probability went up, spot went up
            for i, point in enumerate(history):
                ts = int(point["t"])
                # Linear interpolation of spot based on market probability
                frac = i / max(len(history) - 1, 1)
                p = float(point["p"])
                # Spot move proportional to probability deviation from 50%
                spot_move = (p - 0.5) * 200  # +/- $100 around base for BTC
                spot_prices.append((ts, base + spot_move))

        events = build_events_from_price_history(market, history, spot_prices)
        all_events.extend(events)
        log.info("  %s: %d price points, %d events", market.slug, len(history), len(events))

    all_events.sort(key=lambda e: e.ts)
    return all_events


async def run_backtest(strategy_name: str, market_count: int = 10, bankroll: float = 10000.0):
    """Run a single backtest."""
    if strategy_name not in STRATEGIES:
        print(f"Unknown strategy: {strategy_name}")
        print(f"Available: {', '.join(STRATEGIES.keys())}")
        return

    print(f"\nDiscovering recent resolved markets (last {market_count} windows per asset)...")
    markets = await discover_resolved_markets(count_per_asset=market_count)

    if not markets:
        print("No markets found. The API may be rate-limiting or markets are unavailable.")
        return

    print(f"Found {len(markets)} markets. Fetching price history...")
    events = await fetch_backtest_data(markets)

    if not events:
        print("No events to replay. Check API responses.")
        return

    print(f"Replaying {len(events)} events across {len(markets)} markets...")

    strategy = STRATEGIES[strategy_name]()
    portfolio = Portfolio(bankroll=bankroll)
    runner = StrategyRunner(strategy, portfolio)
    runner.run(events)

    metrics = compute_metrics(portfolio)
    print_summary(strategy_name, metrics, portfolio)

    return metrics


async def run_all(market_count: int = 10, bankroll: float = 10000.0):
    """Run all strategies on the same data and compare."""
    print(f"\nDiscovering recent resolved markets (last {market_count} windows per asset)...")
    markets = await discover_resolved_markets(count_per_asset=market_count)

    if not markets:
        print("No markets found.")
        return

    print(f"Found {len(markets)} markets. Fetching price history...")
    events = await fetch_backtest_data(markets)

    if not events:
        print("No events to replay.")
        return

    print(f"\nRunning all strategies on {len(events)} events...\n")

    results = {}
    for name, factory in STRATEGIES.items():
        strategy = factory()
        portfolio = Portfolio(bankroll=bankroll)
        runner = StrategyRunner(strategy, portfolio)
        runner.run(events)
        metrics = compute_metrics(portfolio)
        results[name] = metrics
        print_summary(name, metrics, portfolio)

    # Comparison table
    print("\n" + "=" * 60)
    print("  STRATEGY COMPARISON")
    print("=" * 60)
    print(f"  {'Strategy':<20} {'Net PnL':>10} {'Trades':>8} {'Win%':>8} {'MaxDD':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*8} {'-'*8} {'-'*10}")
    for name, m in sorted(results.items(), key=lambda x: x[1]["net_pnl"], reverse=True):
        print(f"  {name:<20} ${m['net_pnl']:>9.2f} {m['trade_count']:>8} {m['win_rate']:>7.1f}% ${m['max_drawdown']:>9.2f}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="PolyApi Backtest Runner")
    parser.add_argument("--strategy", "-s", default="spot_momentum",
                        help=f"Strategy to run ({', '.join(STRATEGIES.keys())})")
    parser.add_argument("--markets", "-m", type=int, default=10,
                        help="Number of past windows to scan per asset (default: 10)")
    parser.add_argument("--bankroll", "-b", type=float, default=10000.0,
                        help="Starting bankroll (default: 10000)")
    parser.add_argument("--all", "-a", action="store_true",
                        help="Run all strategies and compare")
    args = parser.parse_args()

    if args.all:
        asyncio.run(run_all(args.markets, args.bankroll))
    else:
        asyncio.run(run_backtest(args.strategy, args.markets, args.bankroll))


if __name__ == "__main__":
    main()

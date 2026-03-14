"""
Paper Trading CLI.

Runs a strategy against live Polymarket data with simulated fills.
Uses the same StrategyRunner as backtesting — only the data source differs.

Usage:
    python -m cli.paper                                  # default: spot_momentum
    python -m cli.paper --strategy always_yes            # run benchmark live
    python -m cli.paper --bankroll 5000                   # custom bankroll
    python -m cli.paper --duration 3600                   # run for 1 hour
    python -m cli.paper --book-poll 5                     # poll L2 books every 5s
"""

import argparse
import asyncio
import logging
import signal
import sys
import time

from strategies.spot_momentum import SpotMomentumStrategy
from strategies.benchmarks import AlwaysYesStrategy, AlwaysNoStrategy, RandomStrategy
from strategies.quant_models import QuantModelsStrategy
from strategies.fee_extremes import FeeExtremesStrategy
from strategies.time_decay import TimeDecayStrategy
from execution.runner import StrategyRunner
from execution.portfolio import Portfolio
from execution.risk_engine import RiskConfig
from data.live_source import LiveSource
from analysis.metrics import compute_metrics, print_summary

# Import engines
from engines.price_engine import PriceEngine
from engines.market_engine import MarketEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

STRATEGIES = {
    "quant_models": lambda: QuantModelsStrategy(),
    "fee_extremes": lambda: FeeExtremesStrategy(),
    "time_decay": lambda: TimeDecayStrategy(),
    "spot_momentum": lambda: SpotMomentumStrategy(),
    "always_yes": lambda: AlwaysYesStrategy(),
    "always_no": lambda: AlwaysNoStrategy(),
    "random": lambda: RandomStrategy(),
}


async def run_paper(
    strategy_name: str,
    bankroll: float = 10_000.0,
    duration: int = 0,
    book_poll_interval: float = 10.0,
):
    """Run paper trading against live data."""
    if strategy_name not in STRATEGIES:
        print(f"Unknown strategy: {strategy_name}")
        print(f"Available: {', '.join(STRATEGIES.keys())}")
        return

    strategy = STRATEGIES[strategy_name]()
    portfolio = Portfolio(bankroll=bankroll)
    runner = StrategyRunner(strategy, portfolio, RiskConfig())

    # Create engines (independent from app.py — paper trader runs standalone)
    price_engine = PriceEngine()
    market_engine = MarketEngine()

    # Create LiveSource and wire it into engines
    source = LiveSource(book_poll_interval=book_poll_interval)
    source.install(price_engine, market_engine)

    # Start everything
    await price_engine.start()
    await market_engine.start()
    await source.start()

    print()
    print("=" * 60)
    print(f"  PAPER TRADING: {strategy_name}")
    print(f"  Bankroll: ${bankroll:,.2f}")
    print(f"  Duration: {'unlimited' if duration == 0 else f'{duration}s'}")
    print(f"  Book poll: every {book_poll_interval}s")
    print("=" * 60)
    print()
    print("  Waiting for data...")
    print("  (Ctrl+C to stop)")
    print()

    start_time = time.time()
    event_count = 0
    last_status = time.time()
    stop = asyncio.Event()

    # Handle Ctrl+C gracefully
    def on_signal(*_):
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, on_signal)
    except (NotImplementedError, AttributeError):
        # Windows doesn't support add_signal_handler in all cases
        pass

    try:
        async for event in source.events():
            if stop.is_set():
                break

            if duration > 0 and (time.time() - start_time) > duration:
                break

            runner._process_event(event)
            event_count += 1

            # Print status every 30 seconds
            if time.time() - last_status > 30:
                last_status = time.time()
                elapsed = time.time() - start_time
                port = portfolio.summary()
                open_pos = port["open_positions"]
                settled = port["settled_count"]
                pnl = port["net_pnl"]
                print(f"  [{elapsed/60:.0f}m] events={event_count}  "
                      f"open={open_pos}  settled={settled}  "
                      f"pnl=${pnl:.2f}  bankroll=${portfolio.bankroll:.2f}")

    except KeyboardInterrupt:
        pass
    finally:
        print("\n  Stopping...")
        await source.stop()
        await price_engine.stop()
        await market_engine.stop()

    # Final summary
    metrics = compute_metrics(portfolio)
    print_summary(strategy_name, metrics, portfolio)

    elapsed = time.time() - start_time
    print(f"\n  Session: {elapsed/60:.1f} minutes, {event_count} events processed")

    # Print individual trades
    if portfolio.trades:
        print(f"\n  Trade Log ({len(portfolio.trades)} trades):")
        for t in portfolio.trades:
            print(f"    {t.side:<10} {t.asset} {t.size:.0f} @ ${t.fill_price:.4f}  "
                  f"fee=${t.fee:.4f}  slip={t.slippage_bps:.0f}bps  [{t.rationale}]")

    if portfolio.settled:
        print(f"\n  Settlements ({len(portfolio.settled)}):")
        for s in portfolio.settled:
            result = "WIN" if s.pnl > 0 else "LOSS"
            print(f"    {result:<5} {s.position.asset} {s.position.side}  "
                  f"pnl=${s.pnl:.2f}  outcome={s.outcome}")


def main():
    parser = argparse.ArgumentParser(description="PolyApi Paper Trader")
    parser.add_argument("--strategy", "-s", default="spot_momentum",
                        help=f"Strategy ({', '.join(STRATEGIES.keys())})")
    parser.add_argument("--bankroll", "-b", type=float, default=10_000.0,
                        help="Starting bankroll (default: 10000)")
    parser.add_argument("--duration", "-d", type=int, default=0,
                        help="Duration in seconds (0 = unlimited, Ctrl+C to stop)")
    parser.add_argument("--book-poll", type=float, default=10.0,
                        help="L2 book poll interval in seconds (default: 10)")
    args = parser.parse_args()

    try:
        asyncio.run(run_paper(args.strategy, args.bankroll, args.duration, args.book_poll))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

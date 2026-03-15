"""
Replay recorded JSONL data through the backtest engine.

Reads JSONL files produced by the recorder and converts them into
the Event stream that StrategyRunner consumes — same format as
live_source.py and replay_source.py.

Usage:
    python -m collector.replay data_store/2026-03-14.jsonl --strategy quant_models
    python -m collector.replay data_store/ --all                    # all files, all strategies
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import List

from data.models import Event, SpotTick, ClobSnapshot, MarketInfo, MarketResolution
from strategies.quant_models import QuantModelsStrategy
from strategies.fee_extremes import FeeExtremesStrategy
from strategies.time_decay import TimeDecayStrategy
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.liquidity_vacuum import LiquidityVacuumStrategy
from strategies.consensus import ConsensusStrategy
from strategies.spot_momentum import SpotMomentumStrategy
from strategies.benchmarks import AlwaysYesStrategy, AlwaysNoStrategy, RandomStrategy
from execution.runner import StrategyRunner
from execution.portfolio import Portfolio
from execution.risk_engine import RiskConfig
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


def parse_levels(raw: list) -> list:
    """Parse order book levels from various CLOB formats."""
    levels = []
    for item in raw:
        if isinstance(item, dict):
            p = float(item.get("price", 0))
            s = float(item.get("size", 0))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            p = float(item[0])
            s = float(item[1])
        else:
            continue
        if p > 0 and s > 0:
            levels.append((p, s))
    return levels


def load_events(paths: List[Path]) -> List[Event]:
    """Load JSONL files and convert to Event objects."""
    events = []
    known_cids = {}  # token_id -> condition_id

    for path in sorted(paths):
        log.info("Loading %s", path)
        with open(path) as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = raw.get("t", 0)
                etype = raw.get("type", "")

                if etype == "spot" or etype == "chainlink":
                    # Treat chainlink oracle prices same as spot ticks
                    events.append(Event(
                        ts=ts, type="spot",
                        spot=SpotTick(ts=ts, symbol=raw["sym"], price=raw["price"]),
                    ))

                elif etype == "market_info":
                    d = raw.get("data", {})
                    cid = d.get("condition_id", "")
                    tid = d.get("yes_token_id", "")
                    if cid and tid:
                        known_cids[tid] = cid
                        info = MarketInfo(
                            condition_id=cid,
                            yes_token_id=tid,
                            asset=d.get("asset", ""),
                            slug=d.get("slug", ""),
                            window_start_ts=d.get("window_start_ts", 0),
                            window_end_ts=d.get("window_end_ts", 0),
                            question=d.get("question", ""),
                            volume=d.get("volume", 0),
                            liquidity=d.get("liquidity", 0),
                        )
                        events.append(Event(ts=ts, type="market_info", market_info=info))

                elif etype == "clob":
                    data = raw.get("data", {})
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        et = item.get("event_type", "")
                        aid = item.get("asset_id", "")
                        cid = known_cids.get(aid, "")

                        if et == "book":
                            bids = parse_levels(item.get("bids", []))
                            asks = parse_levels(item.get("asks", []))
                            bb = bids[0][0] if bids else 0.0
                            ba = asks[0][0] if asks else 1.0
                            events.append(Event(
                                ts=ts, type="clob",
                                clob=ClobSnapshot(
                                    ts=ts, asset_id=aid, condition_id=cid,
                                    bids=bids, asks=asks,
                                    best_bid=bb, best_ask=ba,
                                    last_trade_price=float(item["last_trade_price"]) if item.get("last_trade_price") else None,
                                ),
                            ))

                        elif et == "best_bid_ask":
                            bid = item.get("best_bid")
                            ask = item.get("best_ask")
                            if bid and ask:
                                events.append(Event(
                                    ts=ts, type="clob",
                                    clob=ClobSnapshot(
                                        ts=ts, asset_id=aid, condition_id=cid,
                                        bids=[(float(bid), 100.0)],
                                        asks=[(float(ask), 100.0)],
                                        best_bid=float(bid), best_ask=float(ask),
                                    ),
                                ))

                        elif et == "last_trade_price":
                            price = item.get("price")
                            if price:
                                events.append(Event(
                                    ts=ts, type="clob",
                                    clob=ClobSnapshot(
                                        ts=ts, asset_id=aid, condition_id=cid,
                                        bids=[], asks=[],
                                        best_bid=0.0, best_ask=1.0,
                                        last_trade_price=float(price),
                                    ),
                                ))

                        elif et == "price_change":
                            for ch in item.get("price_changes", []):
                                ch_aid = ch.get("asset_id", "")
                                ch_cid = known_cids.get(ch_aid, "")
                                bid = ch.get("best_bid")
                                ask = ch.get("best_ask")
                                price = ch.get("price")
                                if bid and ask:
                                    events.append(Event(
                                        ts=ts, type="clob",
                                        clob=ClobSnapshot(
                                            ts=ts, asset_id=ch_aid, condition_id=ch_cid,
                                            bids=[(float(bid), 100.0)],
                                            asks=[(float(ask), 100.0)],
                                            best_bid=float(bid), best_ask=float(ask),
                                            last_trade_price=float(price) if price else None,
                                        ),
                                    ))

                elif etype == "resolution":
                    cid = raw.get("condition_id", "")
                    outcome = raw.get("outcome", "no")
                    events.append(Event(
                        ts=ts, type="resolution",
                        resolution=MarketResolution(ts=ts, condition_id=cid, outcome=outcome),
                    ))

    events.sort(key=lambda e: e.ts)
    log.info("Loaded %d events total", len(events))
    return events


def run_replay(events: List[Event], strategy_name: str, bankroll: float = 10_000.0):
    """Run a strategy on recorded events."""
    if strategy_name not in STRATEGIES:
        print(f"Unknown strategy: {strategy_name}")
        return None

    strategy = STRATEGIES[strategy_name]()
    portfolio = Portfolio(bankroll=bankroll)
    runner = StrategyRunner(strategy, portfolio, RiskConfig())
    runner.run(events)
    metrics = compute_metrics(portfolio)
    print_summary(strategy_name, metrics, portfolio)
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Replay recorded data through strategies")
    parser.add_argument("path", help="JSONL file or directory of JSONL files")
    parser.add_argument("--strategy", "-s", default="quant_models",
                        help=f"Strategy ({', '.join(STRATEGIES.keys())})")
    parser.add_argument("--bankroll", "-b", type=float, default=10_000.0)
    parser.add_argument("--all", "-a", action="store_true",
                        help="Run all strategies and compare")
    args = parser.parse_args()

    p = Path(args.path)
    if p.is_dir():
        files = sorted(p.glob("*.jsonl"))
    elif p.is_file():
        files = [p]
    else:
        print(f"Not found: {args.path}")
        return

    if not files:
        print("No JSONL files found")
        return

    print(f"Loading {len(files)} file(s)...")
    events = load_events(files)

    if not events:
        print("No events loaded")
        return

    # Count event types
    spot_count = sum(1 for e in events if e.type == "spot")
    clob_count = sum(1 for e in events if e.type == "clob")
    market_count = sum(1 for e in events if e.type == "market_info")
    res_count = sum(1 for e in events if e.type == "resolution")
    print(f"Events: {len(events)} total (spot={spot_count} clob={clob_count} "
          f"markets={market_count} resolutions={res_count})")

    if args.all:
        results = {}
        for name in STRATEGIES:
            strategy = STRATEGIES[name]()
            portfolio = Portfolio(bankroll=args.bankroll)
            runner = StrategyRunner(strategy, portfolio, RiskConfig())
            runner.run(events)
            metrics = compute_metrics(portfolio)
            results[name] = metrics
            print_summary(name, metrics, portfolio)

        print("\n" + "=" * 70)
        print("  STRATEGY COMPARISON (recorded data)")
        print("=" * 70)
        print(f"  {'Strategy':<20} {'Net PnL':>10} {'Trades':>8} {'Win%':>8} {'MaxDD':>10} {'PF':>8}")
        print(f"  {'-'*20} {'-'*10} {'-'*8} {'-'*8} {'-'*10} {'-'*8}")
        for name, m in sorted(results.items(), key=lambda x: x[1]["net_pnl"], reverse=True):
            print(f"  {name:<20} ${m['net_pnl']:>9.2f} {m['trade_count']:>8} "
                  f"{m['win_rate']:>7.1f}% ${m['max_drawdown']:>9.2f} {m['profit_factor']:>7.2f}")
        print("=" * 70)
    else:
        run_replay(events, args.strategy, args.bankroll)


if __name__ == "__main__":
    main()

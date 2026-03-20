"""
Replay recorded data through the backtest engine.

Supports JSONL (raw or filtered) and Parquet (preprocessed) formats.
Use `python -m collector.preprocess` to convert raw JSONL to fast formats.

Usage:
    python -m collector.replay data_store/replay_data.parquet --all   # fast (preprocessed)
    python -m collector.replay data_store/replay_filtered.jsonl --all # fast (filtered)
    python -m collector.replay data_store/ --all                      # slow (raw JSONL)
"""

import argparse
import logging
import time as _time
from pathlib import Path
from typing import List

try:
    import orjson
    def _json_loads(s):
        return orjson.loads(s)
except ImportError:
    import json
    def _json_loads(s):
        return json.loads(s)

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


def run_columnar_replay(path, strategy_names, bankroll=10_000.0):
    """Direct-from-columnar replay — skips Event/SpotTick object creation.

    Instead of building ~9.6M Event objects, directly updates StrategyRunner
    internal state from Parquet column arrays. Only creates ClobSnapshot when
    a registered market needs strategy evaluation (~0.1% of rows).
    """
    import pyarrow.parquet as pq

    log.info("Loading Parquet: %s", path)
    t0 = _time.time()
    table = pq.read_table(str(path))
    n = len(table)
    log.info("Parquet read: %d rows in %.1fs", n, _time.time() - t0)

    # Sort by timestamp for correct event ordering
    log.info("Sorting by timestamp...")
    ts0 = _time.time()
    table = table.sort_by("ts")
    log.info("Sort: %.1fs", _time.time() - ts0)

    # Bulk-convert columns to Python lists (faster than per-row .as_py())
    log.info("Converting columns to Python lists...")
    t1 = _time.time()
    type_col = table.column("type").to_pylist()
    ts_col = table.column("ts").to_pylist()
    sym_col = table.column("sym").to_pylist()
    price_col = table.column("price").to_pylist()
    aid_col = table.column("asset_id").to_pylist()
    cid_col = table.column("condition_id").to_pylist()
    et_col = table.column("event_type").to_pylist()
    bb_col = table.column("best_bid").to_pylist()
    ba_col = table.column("best_ask").to_pylist()
    ltp_col = table.column("last_trade_price").to_pylist()
    bids_json_col = table.column("bids_json").to_pylist()
    asks_json_col = table.column("asks_json").to_pylist()
    mi_cid_col = table.column("mi_condition_id").to_pylist()
    mi_tid_col = table.column("mi_yes_token_id").to_pylist()
    mi_asset_col = table.column("mi_asset").to_pylist()
    mi_slug_col = table.column("mi_slug").to_pylist()
    mi_wstart_col = table.column("mi_window_start_ts").to_pylist()
    mi_wend_col = table.column("mi_window_end_ts").to_pylist()
    mi_question_col = table.column("mi_question").to_pylist()
    mi_volume_col = table.column("mi_volume").to_pylist()
    mi_liquidity_col = table.column("mi_liquidity").to_pylist()
    res_cid_col = table.column("res_condition_id").to_pylist()
    res_outcome_col = table.column("res_outcome").to_pylist()
    del table
    log.info("Column conversion: %.1fs", _time.time() - t1)

    # Build runners
    runners = {}
    portfolios = {}
    for name in strategy_names:
        if name not in STRATEGIES:
            log.warning("Unknown strategy: %s — skipping", name)
            continue
        strategy = STRATEGIES[name]()
        portfolio = Portfolio(bankroll=bankroll)
        runner = StrategyRunner(strategy, portfolio, RiskConfig())
        runners[name] = runner
        portfolios[name] = portfolio

    if not runners:
        log.error("No valid strategies")
        return {}, {}

    runner_list = list(runners.values())
    token_to_cid = {}  # asset_id (yes_token_id) -> condition_id

    n_spot = 0
    n_clob = 0
    n_clob_eval = 0
    n_market = 0
    n_res = 0

    log.info("Running %d strategies over %d rows (columnar, no Event objects)...",
             len(runners), n)
    t2 = _time.time()

    for i in range(n):
        etype = type_col[i]

        if etype == "spot" or etype == "chainlink":
            sym = sym_col[i]
            price = price_col[i]
            if sym and price is not None:
                for r in runner_list:
                    r._spot_prices[sym] = price
                n_spot += 1

        elif etype == "market_info":
            cid = mi_cid_col[i] or ""
            tid = mi_tid_col[i] or ""
            if cid and tid:
                token_to_cid[tid] = cid
                info = MarketInfo(
                    condition_id=cid,
                    yes_token_id=tid,
                    asset=mi_asset_col[i] or "",
                    slug=mi_slug_col[i] or "",
                    window_start_ts=mi_wstart_col[i] or 0,
                    window_end_ts=mi_wend_col[i] or 0,
                    question=mi_question_col[i] or "",
                    volume=mi_volume_col[i] or 0,
                    liquidity=mi_liquidity_col[i] or 0,
                )
                for r in runner_list:
                    r._handle_market_info(info)
                n_market += 1

        elif etype == "clob":
            aid = aid_col[i] or ""
            cid_val = cid_col[i]
            cid = cid_val if cid_val else token_to_cid.get(aid, "")

            n_clob += 1

            if not cid:
                continue

            # Skip CLOB events for markets no runner cares about
            has_market = False
            for r in runner_list:
                if cid in r._markets:
                    has_market = True
                    break
            if not has_market:
                continue

            # Build ClobSnapshot only when a registered market needs it
            ts = ts_col[i] or 0
            evt = et_col[i] or ""
            bb = bb_col[i]
            ba = ba_col[i]
            ltp = ltp_col[i]

            if evt == "book":
                bids_raw = bids_json_col[i]
                asks_raw = asks_json_col[i]
                bids = parse_levels(_json_loads(bids_raw)) if bids_raw else []
                asks = parse_levels(_json_loads(asks_raw)) if asks_raw else []
                snap = ClobSnapshot(
                    ts=ts, asset_id=aid, condition_id=cid,
                    bids=bids, asks=asks,
                    best_bid=bb or (bids[0][0] if bids else 0.0),
                    best_ask=ba or (asks[0][0] if asks else 1.0),
                    last_trade_price=ltp,
                )
            elif bb is not None and ba is not None:
                snap = ClobSnapshot(
                    ts=ts, asset_id=aid, condition_id=cid,
                    bids=[(float(bb), 100.0)] if bb else [],
                    asks=[(float(ba), 100.0)] if ba else [],
                    best_bid=float(bb) if bb else 0.0,
                    best_ask=float(ba) if ba else 1.0,
                    last_trade_price=ltp,
                )
            elif ltp is not None:
                snap = ClobSnapshot(
                    ts=ts, asset_id=aid, condition_id=cid,
                    bids=[], asks=[],
                    best_bid=0.0, best_ask=1.0,
                    last_trade_price=ltp,
                )
            else:
                continue

            for r in runner_list:
                r._handle_clob(snap)
            n_clob_eval += 1

        elif etype == "resolution":
            cid = res_cid_col[i] or ""
            outcome = res_outcome_col[i] or "no"
            ts = ts_col[i] or 0
            res = MarketResolution(ts=ts, condition_id=cid, outcome=outcome)
            for r in runner_list:
                r._handle_resolution(res)
            n_res += 1

        if i > 0 and i % 2_000_000 == 0:
            log.info("  %d/%d rows (%.0fs) | spot=%d clob=%d(eval=%d) market=%d res=%d",
                     i, n, _time.time() - t2,
                     n_spot, n_clob, n_clob_eval, n_market, n_res)

    elapsed = _time.time() - t2
    log.info("Columnar replay complete: %.1fs for %d rows x %d strategies",
             elapsed, n, len(runners))
    log.info("  spot=%d  clob=%d (eval=%d, skip=%d)  markets=%d  resolutions=%d",
             n_spot, n_clob, n_clob_eval, n_clob - n_clob_eval, n_market, n_res)
    log.info("Total wall time: %.1fs", _time.time() - t0)

    return runners, portfolios


def load_events_parquet(path: Path) -> List[Event]:
    """Load preprocessed Parquet file — bulk column reads for speed."""
    import pyarrow.parquet as pq

    log.info("Loading Parquet: %s", path)
    t0 = _time.time()
    table = pq.read_table(str(path))
    n = len(table)
    log.info("Parquet read: %d rows in %.1fs", n, _time.time() - t0)

    # Bulk-convert all columns to Python lists (much faster than per-row .as_py())
    log.info("Converting columns to Python lists...")
    t1 = _time.time()
    cols = {}
    for name in table.schema.names:
        cols[name] = table.column(name).to_pylist()
    del table  # free memory
    log.info("Column conversion: %.1fs", _time.time() - t1)

    events = []
    known_cids = {}

    log.info("Building Event objects...")
    t2 = _time.time()
    for i in range(n):
        ts = cols["ts"][i] or 0
        etype = cols["type"][i] or ""

        if etype in ("spot", "chainlink"):
            sym = cols["sym"][i]
            price = cols["price"][i]
            if sym and price:
                events.append(Event(
                    ts=ts, type="spot",
                    spot=SpotTick(ts=ts, symbol=sym, price=price),
                ))

        elif etype == "market_info":
            cid = cols["mi_condition_id"][i] or ""
            tid = cols["mi_yes_token_id"][i] or ""
            if cid and tid:
                known_cids[tid] = cid
                events.append(Event(ts=ts, type="market_info", market_info=MarketInfo(
                    condition_id=cid, yes_token_id=tid,
                    asset=cols["mi_asset"][i] or "",
                    slug=cols["mi_slug"][i] or "",
                    window_start_ts=cols["mi_window_start_ts"][i] or 0,
                    window_end_ts=cols["mi_window_end_ts"][i] or 0,
                    question=cols["mi_question"][i] or "",
                    volume=cols["mi_volume"][i] or 0,
                    liquidity=cols["mi_liquidity"][i] or 0,
                )))

        elif etype == "clob":
            aid = cols["asset_id"][i] or ""
            cid_val = cols["condition_id"][i]
            cid = cid_val if cid_val else known_cids.get(aid, "")
            et = cols["event_type"][i] or ""
            bb = cols["best_bid"][i]
            ba = cols["best_ask"][i]
            ltp = cols["last_trade_price"][i]

            if et == "book":
                bids_raw = cols["bids_json"][i]
                asks_raw = cols["asks_json"][i]
                bids = parse_levels(_json_loads(bids_raw)) if bids_raw else []
                asks = parse_levels(_json_loads(asks_raw)) if asks_raw else []
                events.append(Event(ts=ts, type="clob", clob=ClobSnapshot(
                    ts=ts, asset_id=aid, condition_id=cid,
                    bids=bids, asks=asks,
                    best_bid=bb or (bids[0][0] if bids else 0.0),
                    best_ask=ba or (asks[0][0] if asks else 1.0),
                    last_trade_price=ltp,
                )))
            elif bb is not None and ba is not None:
                events.append(Event(ts=ts, type="clob", clob=ClobSnapshot(
                    ts=ts, asset_id=aid, condition_id=cid,
                    bids=[(float(bb), 100.0)] if bb else [],
                    asks=[(float(ba), 100.0)] if ba else [],
                    best_bid=float(bb) if bb else 0.0,
                    best_ask=float(ba) if ba else 1.0,
                    last_trade_price=ltp,
                )))
            elif ltp is not None:
                events.append(Event(ts=ts, type="clob", clob=ClobSnapshot(
                    ts=ts, asset_id=aid, condition_id=cid,
                    bids=[], asks=[], best_bid=0.0, best_ask=1.0,
                    last_trade_price=ltp,
                )))

        elif etype == "resolution":
            cid = cols["res_condition_id"][i] or ""
            outcome = cols["res_outcome"][i] or "no"
            events.append(Event(ts=ts, type="resolution",
                                resolution=MarketResolution(ts=ts, condition_id=cid, outcome=outcome)))

        if i > 0 and i % 2_000_000 == 0:
            log.info("  %d/%d rows processed (%.0fs)", i, n, _time.time() - t2)

    events.sort(key=lambda e: e.ts)
    log.info("Loaded %d events from Parquet in %.1fs total", len(events), _time.time() - t0)
    return events


def load_events(paths: List[Path]) -> List[Event]:
    """Load JSONL files and convert to Event objects."""
    events = []
    known_cids = {}  # token_id -> condition_id

    for path in sorted(paths):
        log.info("Loading %s", path)
        with open(path, "rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = _json_loads(line)
                except Exception:
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


def _print_comparison(results):
    """Print strategy comparison table."""
    print("\n" + "=" * 70)
    print("  STRATEGY COMPARISON (recorded data)")
    print("=" * 70)
    print(f"  {'Strategy':<20} {'Net PnL':>10} {'Trades':>8} {'Win%':>8} {'MaxDD':>10} {'PF':>8}")
    print(f"  {'-'*20} {'-'*10} {'-'*8} {'-'*8} {'-'*10} {'-'*8}")
    for name, m in sorted(results.items(), key=lambda x: x[1]["net_pnl"], reverse=True):
        print(f"  {name:<20} ${m['net_pnl']:>9.2f} {m['trade_count']:>8} "
              f"{m['win_rate']:>7.1f}% ${m['max_drawdown']:>9.2f} {m['profit_factor']:>7.2f}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Replay recorded data through strategies")
    parser.add_argument("path", help="JSONL file, Parquet file, or directory")
    parser.add_argument("--strategy", "-s", default="quant_models",
                        help=f"Strategy ({', '.join(STRATEGIES.keys())})")
    parser.add_argument("--bankroll", "-b", type=float, default=10_000.0)
    parser.add_argument("--all", "-a", action="store_true",
                        help="Run all strategies and compare")
    args = parser.parse_args()

    p = Path(args.path)

    # --- Fast path: Parquet → columnar replay (no Event objects) ---
    parquet_path = None
    if p.suffix == ".parquet" and p.is_file():
        parquet_path = p
    elif p.is_dir():
        candidate = p / "replay_data.parquet"
        if candidate.exists():
            parquet_path = candidate

    if parquet_path:
        names = list(STRATEGIES.keys()) if args.all else [args.strategy]
        runners, portfolios = run_columnar_replay(parquet_path, names, args.bankroll)
        if not runners:
            return

        results = {}
        for name in runners:
            metrics = compute_metrics(portfolios[name])
            results[name] = metrics
            print_summary(name, metrics, portfolios[name])

        if args.all:
            _print_comparison(results)
        return

    # --- Fallback: JSONL → Event-object replay ---
    if p.is_dir():
        filtered = p / "replay_filtered.jsonl"
        if filtered.exists():
            print(f"Loading filtered JSONL: {filtered}")
            events = load_events([filtered])
        else:
            files = sorted(p.glob("*.jsonl"))
            files = [f for f in files if not f.stem.endswith("_filtered")]
            if not files:
                print("No data files found. Run: python -m collector.preprocess data_store/")
                return
            print(f"Loading {len(files)} raw JSONL file(s) (slow — run preprocess first)...")
            events = load_events(files)
    elif p.is_file() and p.suffix == ".jsonl":
        print(f"Loading JSONL: {p}")
        events = load_events([p])
    else:
        print(f"Not found: {args.path}")
        return

    if not events:
        print("No events loaded")
        return

    spot_count = sum(1 for e in events if e.type == "spot")
    clob_count = sum(1 for e in events if e.type == "clob")
    market_count = sum(1 for e in events if e.type == "market_info")
    res_count = sum(1 for e in events if e.type == "resolution")
    print(f"Events: {len(events)} total (spot={spot_count} clob={clob_count} "
          f"markets={market_count} resolutions={res_count})")

    if args.all:
        runners = {}
        portfolios = {}
        for name in STRATEGIES:
            strategy = STRATEGIES[name]()
            portfolio = Portfolio(bankroll=args.bankroll)
            runner = StrategyRunner(strategy, portfolio, RiskConfig())
            runners[name] = runner
            portfolios[name] = portfolio

        log.info("Running %d strategies in single pass over %d events", len(runners), len(events))
        t0 = _time.time()
        for i, event in enumerate(events):
            for runner in runners.values():
                runner._process_event(event)
            if i > 0 and i % 5_000_000 == 0:
                log.info("  processed %d/%d events (%.0fs)", i, len(events), _time.time() - t0)
        elapsed = _time.time() - t0
        log.info("Single-pass complete: %.1fs for %d events x %d strategies",
                 elapsed, len(events), len(runners))

        results = {}
        for name in STRATEGIES:
            metrics = compute_metrics(portfolios[name])
            results[name] = metrics
            print_summary(name, metrics, portfolios[name])

        _print_comparison(results)
    else:
        run_replay(events, args.strategy, args.bankroll)


if __name__ == "__main__":
    main()

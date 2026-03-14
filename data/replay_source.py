"""
ReplaySource: reconstructs an event stream from fetched historical data for backtesting.

Given a list of MarketInfo (resolved markets) and their price histories,
generates a time-ordered sequence of Events that the StrategyRunner can process.
"""

import logging
from typing import AsyncIterator, Dict, List, Optional, Tuple

from data.models import (
    Event, SpotTick, ClobSnapshot, MarketInfo, MarketResolution,
)

log = logging.getLogger(__name__)


def build_events_from_price_history(
    market: MarketInfo,
    price_history: List[dict],
    spot_prices: List[Tuple[int, float]],
) -> List[Event]:
    """
    Build a list of Events from a market's price history and corresponding spot prices.

    Args:
        market: MarketInfo for this market
        price_history: list of {"t": unix_ts, "p": price} from /prices-history
        spot_prices: list of (unix_ts_seconds, price) for the spot asset, sorted by time

    Returns:
        List of Events sorted by timestamp.
    """
    events: List[Event] = []

    # Market info event at window start
    events.append(Event(
        ts=market.window_start_ts * 1000,
        type="market_info",
        market_info=market,
    ))

    # Find spot price at window start
    spot_at_start = _nearest_spot(spot_prices, market.window_start_ts)

    # Generate spot ticks during the window
    sym = f"{market.asset.lower()}usdt"
    for ts_sec, price in spot_prices:
        if market.window_start_ts <= ts_sec <= market.window_end_ts:
            events.append(Event(
                ts=ts_sec * 1000,
                type="spot",
                spot=SpotTick(ts=ts_sec * 1000, symbol=sym, price=price),
            ))

    # Generate CLOB snapshots from price history (synthetic book from midpoint)
    for point in price_history:
        ts_sec = int(point["t"])
        if ts_sec < market.window_start_ts or ts_sec > market.window_end_ts:
            continue

        mid = float(point["p"])
        # Synthetic book: spread of 2 cents around midpoint
        half_spread = 0.01
        best_bid = max(0.01, mid - half_spread)
        best_ask = min(0.99, mid + half_spread)

        # Synthetic depth: 200 contracts at each level
        bids = [(best_bid, 200.0), (best_bid - 0.01, 300.0)]
        asks = [(best_ask, 200.0), (best_ask + 0.01, 300.0)]

        events.append(Event(
            ts=ts_sec * 1000,
            type="clob",
            clob=ClobSnapshot(
                ts=ts_sec * 1000,
                asset_id=market.yes_token_id,
                condition_id=market.condition_id,
                bids=bids,
                asks=asks,
                best_bid=best_bid,
                best_ask=best_ask,
                last_trade_price=mid,
            ),
        ))

    # Resolution event at window end
    spot_at_end = _nearest_spot(spot_prices, market.window_end_ts)
    if spot_at_start is not None and spot_at_end is not None:
        outcome = "yes" if spot_at_end > spot_at_start else "no"
    else:
        outcome = "no"  # default if we can't determine

    events.append(Event(
        ts=market.window_end_ts * 1000,
        type="resolution",
        resolution=MarketResolution(
            ts=market.window_end_ts * 1000,
            condition_id=market.condition_id,
            outcome=outcome,
        ),
    ))

    events.sort(key=lambda e: e.ts)
    return events


def build_replay_stream(
    markets: List[MarketInfo],
    price_histories: Dict[str, List[dict]],
    spot_prices: Dict[str, List[Tuple[int, float]]],
) -> List[Event]:
    """
    Build a full replay stream from multiple markets.

    Args:
        markets: list of MarketInfo to replay
        price_histories: {condition_id: price_history} from /prices-history
        spot_prices: {symbol: [(ts_sec, price), ...]} for BTC/ETH/SOL

    Returns:
        All events merged and sorted by timestamp.
    """
    all_events: List[Event] = []

    for market in markets:
        history = price_histories.get(market.condition_id, [])
        sym = f"{market.asset.lower()}usdt"
        spots = spot_prices.get(sym, [])

        if not history:
            log.warning("No price history for %s (%s), skipping", market.slug, market.condition_id)
            continue

        events = build_events_from_price_history(market, history, spots)
        all_events.extend(events)

    all_events.sort(key=lambda e: e.ts)
    log.info("Built replay stream: %d events from %d markets", len(all_events), len(markets))
    return all_events


def _nearest_spot(spot_prices: List[Tuple[int, float]], target_ts: int) -> Optional[float]:
    """Binary search for the nearest spot price to a target timestamp."""
    if not spot_prices:
        return None

    lo, hi = 0, len(spot_prices) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if spot_prices[mid][0] < target_ts:
            lo = mid + 1
        else:
            hi = mid

    # Check neighbors for closest
    best = lo
    if lo > 0 and abs(spot_prices[lo - 1][0] - target_ts) < abs(spot_prices[lo][0] - target_ts):
        best = lo - 1

    return spot_prices[best][1]

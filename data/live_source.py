"""
LiveSource: adapts existing PriceEngine + MarketEngine into the Event stream
that StrategyRunner consumes.

Hooks into engine callbacks, translates raw data into data.models.Event objects,
and pushes them into an asyncio.Queue for the paper trading runner to consume.

For fill simulation, fetches the live L2 book from Polymarket REST API.
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

from data.models import Event, SpotTick, ClobSnapshot, MarketInfo, MarketResolution

log = logging.getLogger(__name__)

CLOB = "https://clob.polymarket.com"
WINDOW_SEC = 300


class LiveSource:
    """
    Bridges existing engines to the Event-based StrategyRunner.

    Usage:
        source = LiveSource()
        source.install(price_engine, market_engine)
        async for event in source.events():
            runner.process_event(event)
    """

    def __init__(self, book_poll_interval: float = 10.0):
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False
        self._known_markets: Dict[str, MarketInfo] = {}  # condition_id -> MarketInfo
        self._token_to_condition: Dict[str, str] = {}     # token_id -> condition_id
        self._book_poll_interval = book_poll_interval
        self._book_poll_task: Optional[asyncio.Task] = None
        self._resolution_task: Optional[asyncio.Task] = None

    def install(self, price_engine, market_engine):
        """
        Wire into existing engine callbacks.
        Call this BEFORE engines start, or patch callbacks after.
        """
        # Save original callbacks so we can chain
        orig_on_price = price_engine._on_price
        orig_on_market_event = market_engine._on_market_event
        orig_on_markets_update = market_engine._on_markets_update

        def on_price(sym: str, value: float, ts: int):
            # Forward to original callback (for browser feed)
            if orig_on_price:
                try:
                    orig_on_price(sym, value, ts)
                except Exception:
                    pass
            # Emit Event for paper trader
            self._queue.put_nowait(Event(
                ts=ts,
                type="spot",
                spot=SpotTick(ts=ts, symbol=sym, price=value),
            ))

        def on_market_event(data):
            # Forward to original
            if orig_on_market_event:
                try:
                    orig_on_market_event(data)
                except Exception:
                    pass
            # Parse CLOB event into ClobSnapshot
            self._handle_clob_event(data)

        def on_markets_update(markets: list):
            # Forward to original
            if orig_on_markets_update:
                try:
                    orig_on_markets_update(markets)
                except Exception:
                    pass
            # Emit MarketInfo events for new markets
            self._handle_markets_update(markets)

        price_engine._on_price = on_price
        market_engine._on_market_event = on_market_event
        market_engine._on_markets_update = on_markets_update

    async def start(self):
        """Start background tasks (book polling, resolution checking)."""
        self._running = True
        self._book_poll_task = asyncio.create_task(self._book_poll_loop())
        self._resolution_task = asyncio.create_task(self._resolution_loop())
        log.info("LiveSource started")

    async def stop(self):
        """Stop background tasks."""
        self._running = False
        for task in [self._book_poll_task, self._resolution_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("LiveSource stopped")

    async def events(self):
        """Async generator that yields Events as they arrive."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue

    def _handle_clob_event(self, data):
        """Parse raw CLOB WS event into ClobSnapshot Events."""
        now_ms = int(time.time() * 1000)

        items = data if isinstance(data, list) else [data]
        for item in items:
            event_type = item.get("event_type", "")
            asset_id = item.get("asset_id", "")

            if not asset_id:
                # Some events have market-level data with price_changes
                if event_type == "price_change":
                    for ch in item.get("price_changes", []):
                        aid = ch.get("asset_id", "")
                        cid = self._token_to_condition.get(aid, "")
                        if not cid:
                            continue
                        price = ch.get("price")
                        bid = ch.get("best_bid")
                        ask = ch.get("best_ask")
                        if bid and ask:
                            self._queue.put_nowait(Event(
                                ts=now_ms, type="clob",
                                clob=ClobSnapshot(
                                    ts=now_ms, asset_id=aid, condition_id=cid,
                                    bids=[(float(bid), 100.0)],
                                    asks=[(float(ask), 100.0)],
                                    best_bid=float(bid), best_ask=float(ask),
                                    last_trade_price=float(price) if price else None,
                                ),
                            ))
                continue

            cid = self._token_to_condition.get(asset_id, "")

            if event_type == "best_bid_ask":
                bid = item.get("best_bid")
                ask = item.get("best_ask")
                if bid and ask:
                    self._queue.put_nowait(Event(
                        ts=now_ms, type="clob",
                        clob=ClobSnapshot(
                            ts=now_ms, asset_id=asset_id, condition_id=cid,
                            bids=[(float(bid), 100.0)],
                            asks=[(float(ask), 100.0)],
                            best_bid=float(bid), best_ask=float(ask),
                        ),
                    ))

            elif event_type == "book":
                bids_raw = item.get("bids", [])
                asks_raw = item.get("asks", [])
                bids = self._parse_levels(bids_raw)
                asks = self._parse_levels(asks_raw)
                best_bid = bids[0][0] if bids else 0.0
                best_ask = asks[0][0] if asks else 1.0
                self._queue.put_nowait(Event(
                    ts=now_ms, type="clob",
                    clob=ClobSnapshot(
                        ts=now_ms, asset_id=asset_id, condition_id=cid,
                        bids=bids, asks=asks,
                        best_bid=best_bid, best_ask=best_ask,
                    ),
                ))

            elif event_type == "last_trade_price":
                price = item.get("price")
                if price:
                    # Update the last known book with trade price
                    self._queue.put_nowait(Event(
                        ts=now_ms, type="clob",
                        clob=ClobSnapshot(
                            ts=now_ms, asset_id=asset_id, condition_id=cid,
                            bids=[], asks=[],
                            best_bid=0.0, best_ask=1.0,
                            last_trade_price=float(price),
                        ),
                    ))

    def _handle_markets_update(self, markets: list):
        """Convert market list dicts to MarketInfo events."""
        now_ms = int(time.time() * 1000)

        for m in markets:
            cid = m.get("conditionId", "")
            tid = m.get("yesTokenId", "")
            slug = m.get("slug", "")
            if not cid or not tid:
                continue

            # Parse window timestamps from slug
            parts = slug.rsplit("-", 1)
            try:
                window_start = int(parts[1])
            except (IndexError, ValueError):
                continue

            # Track token -> condition mapping
            self._token_to_condition[tid] = cid

            # Only emit if this is a new market
            if cid not in self._known_markets:
                info = MarketInfo(
                    condition_id=cid,
                    yes_token_id=tid,
                    asset=m.get("asset", "?"),
                    slug=slug,
                    window_start_ts=window_start,
                    window_end_ts=window_start + WINDOW_SEC,
                    question=m.get("question", ""),
                    volume=m.get("volume", 0),
                    liquidity=m.get("liquidity", 0),
                )
                self._known_markets[cid] = info
                self._queue.put_nowait(Event(
                    ts=now_ms,
                    type="market_info",
                    market_info=info,
                ))
                log.info("New market: %s %s", info.asset, info.slug)

    async def _book_poll_loop(self):
        """Periodically fetch full L2 books for known markets (fills need depth)."""
        while self._running:
            try:
                await asyncio.sleep(self._book_poll_interval)
                now = int(time.time())

                # Only poll books for markets that haven't expired
                active = {
                    cid: info for cid, info in self._known_markets.items()
                    if info.window_end_ts > now
                }

                if not active:
                    continue

                async with httpx.AsyncClient(timeout=8) as client:
                    for cid, info in active.items():
                        try:
                            resp = await client.get(f"{CLOB}/book",
                                                    params={"token_id": info.yes_token_id})
                            if resp.status_code != 200:
                                continue
                            data = resp.json()
                            bids = self._parse_levels(data.get("bids", []))
                            asks = self._parse_levels(data.get("asks", []))
                            best_bid = bids[0][0] if bids else 0.0
                            best_ask = asks[0][0] if asks else 1.0

                            now_ms = int(time.time() * 1000)
                            self._queue.put_nowait(Event(
                                ts=now_ms, type="clob",
                                clob=ClobSnapshot(
                                    ts=now_ms, asset_id=info.yes_token_id,
                                    condition_id=cid,
                                    bids=bids, asks=asks,
                                    best_bid=best_bid, best_ask=best_ask,
                                    last_trade_price=float(data["last_trade_price"]) if data.get("last_trade_price") else None,
                                ),
                            ))
                        except Exception:
                            pass

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Book poll error")

    async def _resolution_loop(self):
        """Check for expired markets and emit resolution events."""
        while self._running:
            try:
                await asyncio.sleep(5)
                now = int(time.time())
                now_ms = now * 1000
                expired = []

                for cid, info in list(self._known_markets.items()):
                    if info.window_end_ts <= now:
                        expired.append((cid, info))

                for cid, info in expired:
                    # Determine outcome: fetch final price to see if it resolved YES or NO
                    # If market price > 0.5 at expiry, likely resolved YES
                    # More accurate: check if spot went up
                    outcome = await self._determine_outcome(info)

                    self._queue.put_nowait(Event(
                        ts=now_ms,
                        type="resolution",
                        resolution=MarketResolution(
                            ts=now_ms,
                            condition_id=cid,
                            outcome=outcome,
                        ),
                    ))
                    log.info("Market expired: %s %s -> %s", info.asset, info.slug, outcome)
                    del self._known_markets[cid]

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Resolution loop error")

    async def _determine_outcome(self, info: MarketInfo) -> str:
        """
        Determine market outcome by checking final market price.
        If the last trade price or midpoint > 0.5, it resolved YES.
        Fallback: check the CLOB market for resolution status.
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{CLOB}/book",
                                        params={"token_id": info.yes_token_id})
                if resp.status_code == 200:
                    data = resp.json()
                    ltp = data.get("last_trade_price")
                    if ltp:
                        return "yes" if float(ltp) > 0.5 else "no"

                # Fallback: check CLOB market endpoint
                resp2 = await client.get(f"{CLOB}/markets/{info.condition_id}")
                if resp2.status_code == 200:
                    mkt = resp2.json()
                    if mkt.get("closed"):
                        # Check token prices for resolution
                        for tok in mkt.get("tokens", []):
                            if tok.get("token_id") == info.yes_token_id:
                                price = float(tok.get("price", 0.5))
                                return "yes" if price > 0.5 else "no"
        except Exception:
            pass

        return "no"  # conservative default

    @staticmethod
    def _parse_levels(raw: list) -> List[tuple]:
        """Parse order book levels from various formats."""
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

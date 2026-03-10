"""
Coinbase WebSocket client for BTC/ETH/SOL spot prices.
Persistent connection with auto-reconnect and exponential backoff.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, Optional

import websockets

log = logging.getLogger(__name__)

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD"]
# Map Coinbase product_id -> internal symbol
_PRODUCT_TO_SYM = {"BTC-USD": "btcusdt", "ETH-USD": "ethusdt", "SOL-USD": "solusdt"}


class PriceEngine:
    """Streams spot prices from Coinbase and stores latest values."""

    def __init__(self, on_price: Optional[Callable] = None):
        self.prices: Dict[str, dict] = {}  # sym -> {value, ts}
        self._on_price = on_price
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())
        log.info("PriceEngine started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("PriceEngine stopped")

    async def _run(self):
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(COINBASE_WS) as ws:
                    sub = json.dumps({
                        "type": "subscribe",
                        "product_ids": PRODUCTS,
                        "channels": ["ticker"],
                    })
                    await ws.send(sub)
                    log.info("Coinbase WS connected, subscribed to %s", PRODUCTS)
                    backoff = 1.0

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if msg.get("type") != "ticker":
                            continue

                        price_str = msg.get("price")
                        product = msg.get("product_id")
                        if price_str is None or product not in _PRODUCT_TO_SYM:
                            continue

                        sym = _PRODUCT_TO_SYM[product]
                        value = float(price_str)
                        ts = int(time.time() * 1000)
                        self.prices[sym] = {"value": value, "ts": ts}

                        if self._on_price:
                            try:
                                self._on_price(sym, value, ts)
                            except Exception:
                                log.exception("on_price callback error")

            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._running:
                    break
                log.warning("Coinbase WS disconnected, reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

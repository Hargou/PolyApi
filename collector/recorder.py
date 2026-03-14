"""
Data Recorder — standalone collector that records all WebSocket data to JSONL files.

Runs 3 concurrent streams:
  1. Coinbase spot prices (BTC, ETH, SOL)
  2. Polymarket market discovery + CLOB orderbook events
  3. Market resolution detection

Output: one JSONL file per day in data_store/YYYY-MM-DD.jsonl
Each line is a timestamped event that can be replayed through the backtester.

Designed to run 24/7 on a VPS with minimal resources (~50MB RAM, negligible CPU).

Usage:
    python -m collector.recorder                    # default: data_store/
    python -m collector.recorder --output /data     # custom output dir
"""

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("recorder")

# -- External endpoints --
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD"]
ASSETS = ["btc", "eth", "sol"]
PRODUCT_MAP = {"BTC-USD": "btcusdt", "ETH-USD": "ethusdt", "SOL-USD": "solusdt"}
WINDOW_SEC = 300


class DataRecorder:
    """Records all live data streams to JSONL files for offline replay."""

    def __init__(self, output_dir: str = "data_store"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._running = False
        self._file = None
        self._file_date = None
        self._write_count = 0
        self._lock = asyncio.Lock()

        # Market state
        self._known_markets: Dict[str, dict] = {}  # condition_id -> market info
        self._token_ids: Set[str] = set()
        self._token_to_cid: Dict[str, str] = {}    # token_id -> condition_id

        # Stats
        self._spot_count = 0
        self._clob_count = 0
        self._market_count = 0
        self._resolution_count = 0

    async def start(self):
        """Start all recording streams."""
        self._running = True
        log.info("Starting data recorder -> %s", self.output_dir)

        tasks = [
            asyncio.create_task(self._spot_stream()),
            asyncio.create_task(self._discovery_loop()),
            asyncio.create_task(self._clob_stream()),
            asyncio.create_task(self._resolution_loop()),
            asyncio.create_task(self._status_loop()),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if self._file:
                self._file.close()
            log.info("Recorder stopped. Total: spot=%d clob=%d markets=%d resolutions=%d",
                     self._spot_count, self._clob_count, self._market_count, self._resolution_count)

    def stop(self):
        self._running = False

    async def _write_event(self, event: dict):
        """Write a single event to the current day's JSONL file."""
        async with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._file_date != today:
                if self._file:
                    self._file.close()
                path = self.output_dir / f"{today}.jsonl"
                self._file = open(path, "a", buffering=1)  # line-buffered
                self._file_date = today
                log.info("Writing to %s", path)

            line = json.dumps(event, separators=(",", ":"))
            self._file.write(line + "\n")
            self._write_count += 1

    # ---- Stream 1: Coinbase Spot Prices ----

    async def _spot_stream(self):
        """Connect to Coinbase WS and record spot price ticks."""
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(COINBASE_WS, ping_interval=20) as ws:
                    sub = json.dumps({
                        "type": "subscribe",
                        "product_ids": PRODUCTS,
                        "channels": ["ticker"],
                    })
                    await ws.send(sub)
                    log.info("Coinbase connected")
                    backoff = 1

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            if msg.get("type") != "ticker":
                                continue
                            price_str = msg.get("price")
                            product = msg.get("product_id")
                            if not price_str or product not in PRODUCT_MAP:
                                continue

                            ts = int(time.time() * 1000)
                            await self._write_event({
                                "t": ts,
                                "type": "spot",
                                "sym": PRODUCT_MAP[product],
                                "price": float(price_str),
                            })
                            self._spot_count += 1
                        except Exception:
                            pass

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Coinbase disconnected: %s (reconnect in %ds)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    # ---- Stream 2: Market Discovery ----

    async def _discovery_loop(self):
        """Discover active 5-minute markets every 60 seconds."""
        while self._running:
            try:
                await self._discover_markets()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Discovery error")
            await asyncio.sleep(60)

    async def _discover_markets(self):
        """Fetch active markets from Gamma + CLOB APIs."""
        now = int(time.time())
        # Round to nearest 5-min window
        base = (now // WINDOW_SEC) * WINDOW_SEC

        slugs = []
        for asset in ASSETS:
            for offset in range(-2, 5):
                ts = base + offset * WINDOW_SEC
                slugs.append((asset, f"{asset}-updown-5m-{ts}", ts))

        async with httpx.AsyncClient(timeout=10) as client:
            for asset, slug, window_ts in slugs:
                try:
                    resp = await client.get(f"{GAMMA_API}/events/slug/{slug}")
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    markets = data.get("markets", [])
                    if not markets:
                        continue

                    m = markets[0]
                    cid = m.get("conditionId", "")
                    if not cid or cid in self._known_markets:
                        continue

                    # Resolve token ID from CLOB
                    try:
                        clob_resp = await client.get(f"{CLOB_API}/markets/{cid}")
                        if clob_resp.status_code != 200:
                            continue
                        clob_data = clob_resp.json()
                        tokens = clob_data.get("tokens", [])
                        yes_token = None
                        for tok in tokens:
                            outcome = tok.get("outcome", "").lower()
                            if outcome in ("yes", "up"):
                                yes_token = tok.get("token_id", "")
                                break
                        if not yes_token:
                            continue
                    except Exception:
                        continue

                    end_ts = window_ts + WINDOW_SEC
                    if end_ts < now:
                        continue  # already expired

                    market_info = {
                        "condition_id": cid,
                        "yes_token_id": yes_token,
                        "asset": asset.upper(),
                        "slug": slug,
                        "window_start_ts": window_ts,
                        "window_end_ts": end_ts,
                        "question": m.get("question", ""),
                        "volume": m.get("volume", 0),
                        "liquidity": m.get("liquidity", 0),
                    }

                    self._known_markets[cid] = market_info
                    self._token_ids.add(yes_token)
                    self._token_to_cid[yes_token] = cid

                    ts = int(time.time() * 1000)
                    await self._write_event({
                        "t": ts,
                        "type": "market_info",
                        "data": market_info,
                    })
                    self._market_count += 1
                    log.info("New market: %s %s", asset.upper(), slug)

                except httpx.TimeoutException:
                    pass
                except Exception:
                    pass

    # ---- Stream 3: CLOB WebSocket ----

    async def _clob_stream(self):
        """Connect to Polymarket CLOB WS and record orderbook events."""
        backoff = 1
        while self._running:
            # Wait until we have token IDs
            while not self._token_ids and self._running:
                await asyncio.sleep(2)

            if not self._running:
                break

            try:
                ids = list(self._token_ids)
                async with websockets.connect(CLOB_WS, ping_interval=None) as ws:
                    sub = json.dumps({
                        "assets_ids": ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    })
                    await ws.send(sub)
                    log.info("CLOB connected (%d tokens)", len(ids))
                    backoff = 1

                    # Keepalive task
                    async def keepalive():
                        while self._running:
                            try:
                                await ws.send("PING")
                            except Exception:
                                break
                            await asyncio.sleep(9)

                    ka = asyncio.create_task(keepalive())

                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            if raw == "PONG":
                                continue
                            try:
                                msg = json.loads(raw)
                                ts = int(time.time() * 1000)
                                await self._write_event({
                                    "t": ts,
                                    "type": "clob",
                                    "data": msg,
                                })
                                self._clob_count += 1
                            except json.JSONDecodeError:
                                pass

                            # Check if token list changed
                            if set(ids) != self._token_ids:
                                log.info("Token IDs changed, reconnecting CLOB")
                                break
                    finally:
                        ka.cancel()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("CLOB disconnected: %s (reconnect in %ds)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    # ---- Stream 4: Resolution Detection ----

    async def _resolution_loop(self):
        """Check for expired markets and record resolution outcomes."""
        while self._running:
            try:
                now = int(time.time())
                expired = []

                for cid, info in list(self._known_markets.items()):
                    if info["window_end_ts"] <= now:
                        expired.append((cid, info))

                for cid, info in expired:
                    outcome = await self._determine_outcome(info)
                    ts = int(time.time() * 1000)
                    await self._write_event({
                        "t": ts,
                        "type": "resolution",
                        "condition_id": cid,
                        "outcome": outcome,
                        "asset": info["asset"],
                        "slug": info["slug"],
                    })
                    self._resolution_count += 1
                    log.info("Resolved: %s %s -> %s", info["asset"], info["slug"], outcome)

                    # Cleanup
                    tid = info.get("yes_token_id", "")
                    self._token_ids.discard(tid)
                    self._token_to_cid.pop(tid, None)
                    del self._known_markets[cid]

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Resolution loop error")

            await asyncio.sleep(5)

    async def _determine_outcome(self, info: dict) -> str:
        """Check CLOB API for market outcome."""
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(f"{CLOB_API}/book",
                                        params={"token_id": info["yes_token_id"]})
                if resp.status_code == 200:
                    data = resp.json()
                    ltp = data.get("last_trade_price")
                    if ltp:
                        return "yes" if float(ltp) > 0.5 else "no"

                resp2 = await client.get(f"{CLOB_API}/markets/{info['condition_id']}")
                if resp2.status_code == 200:
                    mkt = resp2.json()
                    if mkt.get("closed"):
                        for tok in mkt.get("tokens", []):
                            if tok.get("token_id") == info["yes_token_id"]:
                                price = float(tok.get("price", 0.5))
                                return "yes" if price > 0.5 else "no"
        except Exception:
            pass
        return "no"

    # ---- Status ----

    async def _status_loop(self):
        """Print status every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)
            log.info("STATUS: spot=%d clob=%d markets=%d resolved=%d lines=%d active=%d",
                     self._spot_count, self._clob_count, self._market_count,
                     self._resolution_count, self._write_count, len(self._known_markets))


async def main(output_dir: str = "data_store"):
    recorder = DataRecorder(output_dir=output_dir)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def on_signal(*_):
        stop.set()
        recorder.stop()

    try:
        loop.add_signal_handler(signal.SIGINT, on_signal)
        loop.add_signal_handler(signal.SIGTERM, on_signal)
    except (NotImplementedError, AttributeError):
        pass  # Windows

    task = asyncio.create_task(recorder.start())

    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PolyApi Data Recorder")
    parser.add_argument("--output", "-o", default="data_store",
                        help="Output directory for JSONL files")
    args = parser.parse_args()
    asyncio.run(main(args.output))

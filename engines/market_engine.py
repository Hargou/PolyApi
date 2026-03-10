"""
Market discovery + CLOB WebSocket client.
- Discovery loop: fetches 5-min markets from Gamma/CLOB REST every 60s
- CLOB WebSocket: subscribes to discovered token IDs, forwards events via callback
- Auto-reconnects and resubscribes when market list changes
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import httpx
import websockets

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WINDOW_SEC = 300


def _asset_from_slug(slug: str) -> str:
    s = (slug or "").lower()
    if s.startswith("btc"):
        return "BTC"
    if s.startswith("eth"):
        return "ETH"
    if s.startswith("sol"):
        return "SOL"
    return "?"


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


async def fetch_5m_markets() -> List[Dict]:
    """Fetch active 5-min crypto markets from Gamma, resolve CLOB token IDs."""
    now = int(time.time())
    prefixes = ["btc-updown-5m", "eth-updown-5m", "sol-updown-5m"]
    step = WINDOW_SEC
    slugs = []
    for pfx in prefixes:
        base = (now // step) * step
        for offset in range(0, step * 4, step):
            slugs.append(f"{pfx}-{base - offset}")
            slugs.append(f"{pfx}-{base + step - offset}")
    seen = set()
    slugs = [s for s in slugs if not (s in seen or seen.add(s))]

    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(
            *[client.get(f"{GAMMA}/events/slug/{s}") for s in slugs],
            return_exceptions=True,
        )

    events = []
    seen_ids = set()
    for r in results:
        if isinstance(r, Exception) or r.status_code != 200:
            continue
        try:
            ev = r.json()
            if ev and ev.get("id") and not ev.get("closed") and ev["id"] not in seen_ids:
                seen_ids.add(ev["id"])
                events.append(ev)
        except Exception:
            pass

    events.sort(key=lambda e: e.get("slug", ""), reverse=True)

    markets_out = []
    for ev in events:
        mkts = ev.get("markets", [])
        if not mkts:
            continue
        m0 = mkts[0]
        cid = m0.get("conditionId")
        if not cid:
            continue

        try:
            async with httpx.AsyncClient(timeout=5) as c:
                clob_r = await c.get(f"{CLOB}/markets/{cid}")
            if clob_r.status_code != 200:
                continue
            clob = clob_r.json()
            if clob.get("accepting_orders") is False:
                continue
            yes_token_id = None
            for tok in clob.get("tokens", []):
                outcome = (tok.get("outcome") or "").strip()
                if outcome in ("Yes", "Up"):
                    yes_token_id = tok.get("token_id")
                    break
            if not yes_token_id:
                continue
        except Exception:
            continue

        slug = ev.get("slug", "")
        pw = None
        parts = slug.rsplit("-", 1)
        if len(parts) == 2:
            try:
                pw = int(parts[1])
            except ValueError:
                pass

        end_ts = pw + step if pw else None
        end_iso = _iso(end_ts) if end_ts else ev.get("endDate")
        if end_ts and end_ts < now:
            continue

        vol = float(m0.get("volume") or ev.get("volume") or 0)
        liq = float(m0.get("liquidity") or ev.get("liquidity") or 0)
        yes_pct = None
        try:
            prices = json.loads(m0.get("outcomePrices") or "[]")
            if prices:
                yes_pct = round(float(prices[0]) * 100, 1)
        except Exception:
            pass

        markets_out.append({
            "id": ev.get("id"),
            "question": ev.get("title") or m0.get("question", ""),
            "conditionId": cid,
            "yesTokenId": yes_token_id,
            "asset": _asset_from_slug(slug),
            "slug": slug,
            "endDate": end_iso,
            "volume": vol,
            "liquidity": liq,
            "yes_pct": yes_pct,
        })

    markets_out.sort(key=lambda m: (m.get("endDate") or ""), reverse=True)
    return markets_out


class MarketEngine:
    """Discovers 5-min markets and streams CLOB events."""

    def __init__(self, on_market_event: Optional[Callable] = None,
                 on_markets_update: Optional[Callable] = None):
        self.markets: List[Dict] = []
        self._on_market_event = on_market_event
        self._on_markets_update = on_markets_update
        self._discovery_task: Optional[asyncio.Task] = None
        self._clob_task: Optional[asyncio.Task] = None
        self._running = False
        self._token_ids: List[str] = []
        self._ids_changed = asyncio.Event()

    async def start(self):
        self._running = True
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        self._clob_task = asyncio.create_task(self._clob_loop())
        log.info("MarketEngine started")

    async def stop(self):
        self._running = False
        self._ids_changed.set()
        for t in [self._discovery_task, self._clob_task]:
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        log.info("MarketEngine stopped")

    async def _discovery_loop(self):
        while self._running:
            try:
                markets = await fetch_5m_markets()
                self.markets = markets
                new_ids = [m["yesTokenId"] for m in markets if m.get("yesTokenId")]
                if set(new_ids) != set(self._token_ids):
                    self._token_ids = new_ids
                    self._ids_changed.set()
                    log.info("Market list updated: %d markets, %d tokens", len(markets), len(new_ids))
                if self._on_markets_update:
                    try:
                        self._on_markets_update(markets)
                    except Exception:
                        log.exception("on_markets_update callback error")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Discovery loop error")
            await asyncio.sleep(60)

    async def _clob_loop(self):
        backoff = 1.0
        while self._running:
            # Wait until we have token IDs
            if not self._token_ids:
                self._ids_changed.clear()
                await self._ids_changed.wait()
                if not self._running:
                    break
                continue

            ids_snapshot = list(self._token_ids)
            self._ids_changed.clear()

            try:
                async with websockets.connect(CLOB_WS) as ws:
                    sub = json.dumps({
                        "assets_ids": ids_snapshot,
                        "type": "market",
                        "custom_feature_enabled": True,
                    })
                    await ws.send(sub)
                    log.info("CLOB WS connected, subscribed to %d tokens", len(ids_snapshot))
                    backoff = 1.0

                    last_ping = time.time()

                    while self._running:
                        # Check if market list changed → reconnect with new subscription
                        if self._ids_changed.is_set():
                            log.info("Token IDs changed, reconnecting CLOB WS")
                            break

                        # Keepalive ping
                        if time.time() - last_ping >= 9:
                            await ws.send("PING")
                            last_ping = time.time()

                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            continue

                        if raw == "PONG":
                            continue

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if self._on_market_event:
                            try:
                                self._on_market_event(data)
                            except Exception:
                                log.exception("on_market_event callback error")

            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._running:
                    break
                log.warning("CLOB WS disconnected, reconnecting in %.1fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

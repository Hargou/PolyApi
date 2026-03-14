"""
Fetch historical data from Polymarket APIs.

- /prices-history: 1-minute price candles (free, works for resolved markets with startTs/endTs)
- /book: live L2 order book snapshot (free, current markets only)
- Market discovery via Gamma API slug lookup
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from data.models import SpotTick, ClobSnapshot, MarketInfo, MarketResolution, Event

log = logging.getLogger(__name__)

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
WINDOW_SEC = 300


async def fetch_price_history(
    asset_id: str,
    start_ts: int,
    end_ts: int,
    fidelity: int = 1,
    timeout: int = 15,
) -> List[dict]:
    """
    Fetch price history from Polymarket CLOB API.

    Args:
        asset_id: token ID (yes or no token)
        start_ts: unix timestamp (seconds)
        end_ts: unix timestamp (seconds)
        fidelity: granularity in minutes (default 1)

    Returns:
        List of {"t": unix_ts, "p": price} dicts, sorted by time.

    Note: Use explicit startTs/endTs, NOT interval=all (returns empty for resolved markets).
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{CLOB}/prices-history", params={
            "market": asset_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        })
        resp.raise_for_status()
        data = resp.json()
    return data.get("history", [])


async def fetch_book(token_id: str, timeout: int = 10) -> Optional[ClobSnapshot]:
    """
    Fetch current L2 order book for a token.

    Args:
        token_id: the yes or no token ID

    Returns:
        ClobSnapshot or None if unavailable.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{CLOB}/book", params={"token_id": token_id})
        if resp.status_code != 200:
            return None
        data = resp.json()

    bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
    asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 1.0

    return ClobSnapshot(
        ts=int(time.time() * 1000),
        asset_id=token_id,
        condition_id=data.get("market", ""),
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        last_trade_price=float(data["last_trade_price"]) if data.get("last_trade_price") else None,
    )


async def fetch_market_by_slug(slug: str, timeout: int = 10) -> Optional[MarketInfo]:
    """
    Look up a 5-min crypto market by its slug via Gamma API, then resolve CLOB token ID.

    Args:
        slug: e.g. "btc-updown-5m-1710000000"

    Returns:
        MarketInfo or None if not found / not active.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Fetch event from Gamma
        resp = await client.get(f"{GAMMA}/events/slug/{slug}")
        if resp.status_code != 200:
            return None
        ev = resp.json()
        if not ev or not ev.get("id"):
            return None

        mkts = ev.get("markets", [])
        if not mkts:
            return None
        m0 = mkts[0]
        cid = m0.get("conditionId")
        if not cid:
            return None

        # Resolve CLOB token ID
        clob_resp = await client.get(f"{CLOB}/markets/{cid}")
        if clob_resp.status_code != 200:
            return None
        clob = clob_resp.json()

        yes_token_id = None
        for tok in clob.get("tokens", []):
            outcome = (tok.get("outcome") or "").strip()
            if outcome in ("Yes", "Up"):
                yes_token_id = tok.get("token_id")
                break
        if not yes_token_id:
            return None

    # Parse window start from slug
    parts = slug.rsplit("-", 1)
    try:
        window_start = int(parts[1])
    except (IndexError, ValueError):
        return None

    asset = "BTC" if "btc" in slug else "ETH" if "eth" in slug else "SOL" if "sol" in slug else "?"

    return MarketInfo(
        condition_id=cid,
        yes_token_id=yes_token_id,
        asset=asset,
        slug=slug,
        window_start_ts=window_start,
        window_end_ts=window_start + WINDOW_SEC,
        question=ev.get("title", m0.get("question", "")),
        volume=float(m0.get("volume") or ev.get("volume") or 0),
        liquidity=float(m0.get("liquidity") or ev.get("liquidity") or 0),
    )


async def fetch_recent_slugs(asset: str = "btc", count: int = 20) -> List[str]:
    """
    Generate recent 5-min market slugs for an asset.
    These are time-based slugs that may or may not have active markets.

    Args:
        asset: "btc", "eth", or "sol"
        count: how many past windows to generate

    Returns:
        List of slug strings, newest first.
    """
    now = int(time.time())
    base = (now // WINDOW_SEC) * WINDOW_SEC
    slugs = []
    for i in range(count):
        ts = base - (i * WINDOW_SEC)
        slugs.append(f"{asset}-updown-5m-{ts}")
    return slugs


async def discover_resolved_markets(
    assets: List[str] = None,
    count_per_asset: int = 20,
    max_concurrent: int = 5,
) -> List[MarketInfo]:
    """
    Discover recently resolved 5-min markets across assets.

    Args:
        assets: which assets to scan (default: btc, eth, sol)
        count_per_asset: how many past windows to check per asset
        max_concurrent: max concurrent API requests

    Returns:
        List of MarketInfo for markets that were found.
    """
    if assets is None:
        assets = ["btc", "eth", "sol"]

    all_slugs = []
    for asset in assets:
        slugs = await fetch_recent_slugs(asset, count_per_asset)
        all_slugs.extend(slugs)

    # Fetch in batches to avoid overwhelming the API
    sem = asyncio.Semaphore(max_concurrent)
    markets = []

    async def fetch_one(slug: str):
        async with sem:
            try:
                m = await fetch_market_by_slug(slug)
                if m:
                    markets.append(m)
            except Exception:
                pass

    await asyncio.gather(*[fetch_one(s) for s in all_slugs])
    markets.sort(key=lambda m: m.window_start_ts, reverse=True)
    return markets


def determine_outcome(slug: str, spot_at_start: float, spot_at_end: float) -> str:
    """
    Determine the outcome of a 5-min up/down market.
    "Up" = spot price at end > spot price at start.

    Returns "yes" if price went up, "no" if it went down or stayed flat.
    """
    return "yes" if spot_at_end > spot_at_start else "no"

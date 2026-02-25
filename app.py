"""
Polymarket Crypto 5-Min Prediction Dashboard — FastAPI backend
Proxies Gamma/CLOB APIs and serves the terminal UI.
"""

import asyncio
import json
import time
from typing import List, Dict, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
# Binance may be geo-blocked; CoinGecko used as primary for spot prices
BINANCE_URLS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
]
SPOT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "MATICUSDT", "LINKUSDT"]
COINGECKO_IDS = {"btcusdt": "bitcoin", "ethusdt": "ethereum", "solusdt": "solana", "maticusdt": "matic-network", "linkusdt": "chainlink"}

app = FastAPI(title="Polymarket Crypto 5-Min Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


def _asset_from_slug(slug: str) -> str:
    s = (slug or "").lower()
    if s.startswith("btc"): return "BTC"
    if s.startswith("eth"): return "ETH"
    if s.startswith("sol"): return "SOL"
    if s.startswith("matic"): return "MATIC"
    if s.startswith("link"): return "LINK"
    return "?"


async def _fetch_5m_markets() -> List[Dict]:
    """Fetch active 5-min crypto markets from Gamma, resolve CLOB token IDs."""
    now = int(time.time())
    # 5-min windows: btc, eth, sol (Polymarket may have these)
    prefixes = ["btc-updown-5m", "eth-updown-5m", "sol-updown-5m"]
    step = 300
    slugs = []
    for pfx in prefixes:
        base = (now // step) * step
        for offset in range(0, step * 4, step):
            slugs.append(f"{pfx}-{base - offset}")
            slugs.append(f"{pfx}-{base + step - offset}")
    seen = set()
    slugs = [s for s in slugs if not (s in seen or seen.add(s))]

    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(*[
            client.get(f"{GAMMA}/events/slug/{s}") for s in slugs
        ], return_exceptions=True)

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

    # Resolve CLOB token IDs for Yes/Up outcome
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

        from datetime import datetime, timezone
        def _iso(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

        end_ts = pw + step if pw else None
        end_iso = _iso(end_ts) if end_ts else ev.get("endDate")
        # Skip expired markets
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


@app.get("/api/markets-5m")
async def get_markets_5m():
    """5-min crypto prediction markets with CLOB token IDs for WebSocket subscription."""
    return await _fetch_5m_markets()


@app.get("/api/spot-prices")
async def get_spot_prices():
    """Fetch spot prices from CoinGecko (works globally; Binance may be geo-blocked)."""
    out = {}
    ids = ",".join(COINGECKO_IDS.values())
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": ids, "vs_currencies": "usd"})
            r.raise_for_status()
            data = r.json()
        ts = int(time.time() * 1000)
        for sym, cg_id in COINGECKO_IDS.items():
            p = data.get(cg_id, {}).get("usd")
            if p is not None:
                out[sym] = {"value": float(p), "timestamp": ts}
    except Exception:
        pass
    return out


@app.get("/api/clob/{condition_id}")
async def get_clob_market(condition_id: str):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{CLOB}/markets/{condition_id}")
        r.raise_for_status()
        return r.json()

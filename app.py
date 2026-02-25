"""
Polymarket Explorer — FastAPI backend
Proxies Gamma / CLOB APIs, Kalshi comparison, and relays WebSocket data.
"""

import asyncio
import json
import re
import time
from typing import Optional, List, Dict, Any

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

app = FastAPI(title="Polymarket Explorer")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return f.read()


@app.get("/api/events")
async def get_events(
    limit: int = 50,
    offset: int = 0,
    active: str = "true",
    closed: str = "false",
    tag_id: Optional[str] = None,
    slug: Optional[str] = None,
):
    params = {"limit": limit, "offset": offset, "active": active, "closed": closed}
    if tag_id:
        params["tag_id"] = tag_id
    if slug:
        params["slug"] = slug
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{GAMMA}/events", params=params)
        r.raise_for_status()
        return r.json()


@app.get("/api/markets")
async def get_markets(
    limit: int = 50,
    offset: int = 0,
    active: str = "true",
    closed: str = "false",
):
    params = {"limit": limit, "offset": offset, "active": active, "closed": closed}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{GAMMA}/markets", params=params)
        r.raise_for_status()
        return r.json()


@app.get("/api/tags")
async def get_tags():
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{GAMMA}/tags")
        r.raise_for_status()
        return r.json()


@app.get("/api/event/{slug}")
async def get_event_by_slug(slug: str):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{GAMMA}/events/slug/{slug}")
        r.raise_for_status()
        return r.json()


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1)):
    """Search by trying exact slug first, then falling back to general listing."""
    async with httpx.AsyncClient(timeout=15) as client:
        # Try exact slug match
        slug_r = await client.get(f"{GAMMA}/events/slug/{q}")
        if slug_r.status_code == 200:
            data = slug_r.json()
            if data and isinstance(data, dict) and data.get("id"):
                return [data]

        # Try slug param match
        r = await client.get(f"{GAMMA}/events", params={"slug": q, "limit": 10})
        if r.status_code == 200 and r.json():
            return r.json()

        # Fetch a broad set and filter client-side (Gamma API has no text search)
        r = await client.get(f"{GAMMA}/events", params={"active": "true", "closed": "false", "limit": 100})
        r.raise_for_status()
        events = r.json()
        ql = q.lower()
        filtered = [
            e for e in events
            if ql in (e.get("title", "") or "").lower()
            or ql in (e.get("slug", "") or "").lower()
            or any(ql in (m.get("question", "") or "").lower() for m in e.get("markets", []))
        ]
        return filtered[:30]


@app.get("/api/live-crypto")
async def live_crypto():
    """Auto-discover current BTC/ETH Up-or-Down micro-markets by time."""
    now = int(time.time())
    prefixes = ["btc-updown-5m", "btc-updown-15m", "btc-updown-1h",
                "eth-updown-5m", "eth-updown-15m", "eth-updown-1h"]
    intervals = {"5m": 300, "15m": 900, "1h": 3600}

    slugs = []
    for pfx in prefixes:
        interval_key = pfx.split("-")[-1]
        step = intervals.get(interval_key, 300)
        base = (now // step) * step
        for offset in range(0, step * 4, step):
            slugs.append(f"{pfx}-{base - offset}")
            slugs.append(f"{pfx}-{base + step - offset}")

    seen = set()
    slugs = [s for s in slugs if not (s in seen or seen.add(s))]

    events = []
    async with httpx.AsyncClient(timeout=10) as client:
        async def try_slug(slug):
            try:
                r = await client.get(f"{GAMMA}/events/slug/{slug}")
                if r.status_code == 200:
                    ev = r.json()
                    if ev and ev.get("id") and not ev.get("closed"):
                        return ev
            except Exception:
                pass
            return None

        results = await asyncio.gather(*[try_slug(s) for s in slugs])
        seen_ids = set()
        for ev in results:
            if ev and ev["id"] not in seen_ids:
                seen_ids.add(ev["id"])
                events.append(ev)

    events.sort(key=lambda e: e.get("slug", ""), reverse=True)
    return events


# --------------- Kalshi API ---------------

async def _fetch_kalshi_events(client: httpx.AsyncClient, limit: int = 400) -> List[Dict]:
    events: List[Dict] = []
    cursor = ""
    while len(events) < limit:
        params: Dict[str, Any] = {
            "status": "open", "limit": 200, "with_nested_markets": "true",
        }
        if cursor:
            params["cursor"] = cursor
        r = await client.get(f"{KALSHI}/events", params=params)
        r.raise_for_status()
        data = r.json()
        batch = data.get("events", [])
        if not batch:
            break
        events.extend(batch)
        cursor = data.get("cursor", "")
        if not cursor:
            break
    return events


@app.get("/api/kalshi/events")
async def kalshi_events(limit: int = 100):
    async with httpx.AsyncClient(timeout=20) as client:
        evs = await _fetch_kalshi_events(client, limit)
    return evs[:limit]


@app.get("/api/kalshi/markets")
async def kalshi_markets(event_ticker: Optional[str] = None, limit: int = 50):
    params: Dict[str, Any] = {"status": "open", "limit": limit}
    if event_ticker:
        params["event_ticker"] = event_ticker
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{KALSHI}/markets", params=params)
        r.raise_for_status()
        return r.json().get("markets", [])


# --------------- Cross-platform comparison ---------------

from rapidfuzz import fuzz

def _clean(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9 ]", " ", text).strip()

def _event_title(ev: Dict) -> str:
    return _clean(ev.get("title", ""))

def _event_text(ev: Dict, platform: str) -> str:
    title = ev.get("title", "")
    if platform == "poly":
        questions = " ".join(m.get("question", "") for m in ev.get("markets", [])[:3])
    else:
        questions = " ".join(m.get("title", "") for m in ev.get("markets", [])[:3])
    return _clean(f"{title} {questions}")

_GENERIC = {"presidential","election","president","senator","senate","governor",
            "party","win","winner","released","price","before","country","will"}

def _has_entity_overlap(a: str, b: str) -> bool:
    """Check if the two texts share at least one specific named entity / proper noun."""
    words_a = {w.lower() for w in a.split() if len(w) > 2 and w.lower() not in _GENERIC}
    words_b = {w.lower() for w in b.split() if len(w) > 2 and w.lower() not in _GENERIC}
    overlap = words_a & words_b
    return len(overlap) >= 2

def _compare_events(poly_ev: Dict, kalshi_ev: Dict) -> float:
    p_title = _event_title(poly_ev)
    k_title = _event_title(kalshi_ev)
    p_full = _event_text(poly_ev, "poly")
    k_full = _event_text(kalshi_ev, "kalshi")

    title_tsr = fuzz.token_set_ratio(p_title, k_title)
    title_tsor = fuzz.token_sort_ratio(p_title, k_title)
    full_tsr = fuzz.token_set_ratio(p_full, k_full)

    score = max(title_tsr, title_tsor, full_tsr)

    if score >= 75 and not _has_entity_overlap(p_title, k_title):
        score = min(score, 74)

    return score


def _poly_yes_pct(ev: Dict) -> Optional[float]:
    mkts = ev.get("markets", [])
    if not mkts:
        return None
    try:
        prices = json.loads(mkts[0].get("outcomePrices", "[]"))
        if prices:
            return round(float(prices[0]) * 100, 1)
    except Exception:
        pass
    return None


def _kalshi_yes_pct(ev: Dict) -> Optional[float]:
    mkts = ev.get("markets", [])
    if not mkts:
        return None
    m = mkts[0]
    yb = m.get("yes_bid", 0) or 0
    ya = m.get("yes_ask", 0) or 0
    if ya > 0:
        return round((yb + ya) / 2, 1)
    if yb > 0:
        return float(yb)
    return None


@app.get("/api/compare")
async def compare_platforms(min_score: float = 75, poly_limit: int = 100, kalshi_limit: int = 600):
    """Find matching events across Polymarket and Kalshi, show price diffs."""
    async with httpx.AsyncClient(timeout=25) as client:
        poly_task = client.get(
            f"{GAMMA}/events",
            params={"active": "true", "closed": "false", "limit": poly_limit},
        )
        kalshi_task = _fetch_kalshi_events(client, kalshi_limit)
        poly_r, kalshi_evs = await asyncio.gather(poly_task, kalshi_task)
        poly_r.raise_for_status()
        poly_evs = poly_r.json()

    matches = []
    used_kalshi = set()

    for pev in poly_evs:
        best_score, best_kev = 0.0, None
        for kev in kalshi_evs:
            kid = kev.get("event_ticker", "")
            if kid in used_kalshi:
                continue
            score = _compare_events(pev, kev)
            if score > best_score:
                best_score = score
                best_kev = kev

        if best_score >= min_score and best_kev:
            used_kalshi.add(best_kev.get("event_ticker", ""))
            poly_pct = _poly_yes_pct(pev)
            kalshi_pct = _kalshi_yes_pct(best_kev)
            diff = None
            if poly_pct is not None and kalshi_pct is not None:
                diff = round(abs(poly_pct - kalshi_pct), 1)

            matches.append({
                "score": round(best_score),
                "diff": diff,
                "poly": {
                    "title": pev.get("title"),
                    "slug": pev.get("slug"),
                    "yes_pct": poly_pct,
                    "volume": pev.get("volume"),
                    "liquidity": pev.get("liquidity"),
                    "markets_count": len(pev.get("markets", [])),
                    "first_question": (pev.get("markets", [{}])[0].get("question") if pev.get("markets") else None),
                },
                "kalshi": {
                    "title": best_kev.get("title"),
                    "event_ticker": best_kev.get("event_ticker"),
                    "category": best_kev.get("category"),
                    "yes_pct": kalshi_pct,
                    "volume": sum(m.get("volume", 0) for m in best_kev.get("markets", [])),
                    "markets_count": len(best_kev.get("markets", [])),
                    "first_title": (best_kev.get("markets", [{}])[0].get("title") if best_kev.get("markets") else None),
                },
            })

    matches.sort(key=lambda m: (m["score"], m["diff"] or 0), reverse=True)
    return matches


@app.get("/api/clob/{condition_id}")
async def get_clob_market(condition_id: str):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{CLOB}/markets/{condition_id}")
        r.raise_for_status()
        return r.json()


@app.websocket("/ws/prices")
async def ws_prices(ws: WebSocket):
    """Browser connects here. We relay live data from Polymarket's Market WS."""
    await ws.accept()
    try:
        init = await ws.receive_json()
        asset_ids = init.get("asset_ids", [])
        if not asset_ids:
            await ws.send_json({"error": "no asset_ids"})
            return

        async with websockets.connect(MARKET_WS) as upstream:
            await upstream.send(json.dumps({
                "assets_ids": asset_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }))

            last_ping = time.time()
            while True:
                if time.time() - last_ping >= 9:
                    await upstream.send("PING")
                    last_ping = time.time()
                try:
                    raw = await asyncio.wait_for(upstream.recv(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                if raw == "PONG":
                    continue
                await ws.send_text(raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)

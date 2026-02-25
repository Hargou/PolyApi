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
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


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


# Polymarket crypto window configs: (prefix, step_seconds)
_CRYPTO_WINDOWS = [
    ("btc-updown-5m", 300),
    ("eth-updown-5m", 300),
    ("btc-updown-10m", 600),
    ("eth-updown-10m", 600),
    ("btc-updown-15m", 900),
    ("eth-updown-15m", 900),
]


async def _build_poly_crypto() -> List[Dict]:
    """Polymarket-only crypto markets: 5m, 10m, 15m BTC/ETH."""
    now = int(time.time())
    slugs = []
    for pfx, step in _CRYPTO_WINDOWS:
        base = (now // step) * step
        for offset in range(0, step * 4, step):
            slugs.append(f"{pfx}-{base - offset}")
            slugs.append(f"{pfx}-{base + step - offset}")
    seen: set = set()
    slugs = [s for s in slugs if not (s in seen or seen.add(s))]

    async with httpx.AsyncClient(timeout=10) as client:
        poly_results = await asyncio.gather(*[
            client.get(f"{GAMMA}/events/slug/{s}") for s in slugs
        ], return_exceptions=True)

    poly_events = []
    seen_ids: set = set()
    for r in poly_results:
        if isinstance(r, Exception) or r.status_code != 200:
            continue
        try:
            ev = r.json()
            if ev and ev.get("id") and not ev.get("closed") and ev["id"] not in seen_ids:
                seen_ids.add(ev["id"])
                poly_events.append(ev)
        except Exception:
            pass
    poly_events.sort(key=lambda e: e.get("slug", ""), reverse=True)

    def _asset(ev: Dict) -> str:
        s = ev.get("slug", "")
        return "BTC" if s.startswith("btc") else "ETH" if s.startswith("eth") else "?"

    def _window(ev: Dict) -> Optional[int]:
        parts = ev.get("slug", "").rsplit("-", 1)
        if len(parts) == 2:
            try: return int(parts[1])
            except ValueError: pass
        return None

    def _step_from_slug(slug: str) -> int:
        if "5m" in slug: return 300
        if "10m" in slug: return 600
        return 900

    def _up_pct(ev: Dict) -> Optional[float]:
        mkts = ev.get("markets", [])
        if not mkts: return None
        try:
            prices = json.loads(mkts[0].get("outcomePrices", "[]"))
            if prices: return round(float(prices[0]) * 100, 1)
        except Exception: pass
        return None

    def _window_iso(ts: int) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    pairs = []
    for pev in poly_events:
        pw = _window(pev)
        step = _step_from_slug(pev.get("slug", ""))
        poly_m0 = pev.get("markets", [{}])[0] if pev.get("markets") else {}
        pairs.append({
            "asset": _asset(pev),
            "window_start": _window_iso(pw) if pw else None,
            "window_end": _window_iso(pw + step) if pw else None,
            "window_min": step // 60,
            "matched": False,
            "poly": {
                "title": pev.get("title"),
                "slug": pev.get("slug"),
                "yes_pct": _up_pct(pev),
                "conditionId": poly_m0.get("conditionId"),
            },
            "kalshi": None,
            "gap": None,
        })

    pairs.sort(key=lambda p: p.get("window_start") or "", reverse=True)
    return pairs


async def _build_crypto_pairs() -> List[Dict]:
    """Legacy: Poly + Kalshi pairs (for /api/live-crypto-compare)."""
    return await _build_poly_crypto()


@app.get("/api/live-crypto-compare")
async def live_crypto_compare():
    """REST endpoint — one-shot snapshot."""
    return await _build_crypto_pairs()


@app.websocket("/ws/crypto-arb")
async def ws_crypto_arb(ws: WebSocket):
    """Stream live Polymarket crypto prices (5m, 10m, 15m BTC/ETH) via WebSocket."""
    await ws.accept()
    stop = asyncio.Event()

    try:
        await ws.send_json({"type": "connected", "ts": time.time()})
        pairs = await _build_poly_crypto()
        await ws.send_json({"type": "snapshot", "pairs": pairs, "ts": time.time()})

        token_map: Dict[str, tuple] = {}
        cond_to_idx: Dict[str, int] = {}
        cids = []
        for i, p in enumerate(pairs):
            poly = p.get("poly")
            if poly and poly.get("conditionId"):
                cid = poly["conditionId"]
                cids.append(cid)
                cond_to_idx[cid] = i

        if cids:
            async with httpx.AsyncClient(timeout=10) as client:
                resps = await asyncio.gather(
                    *[client.get(f"{CLOB}/markets/{c}") for c in cids],
                    return_exceptions=True,
                )
                for cid, resp in zip(cids, resps):
                    if isinstance(resp, Exception) or resp.status_code != 200:
                        continue
                    mkt = resp.json()
                    for tok in mkt.get("tokens", []):
                        tid = tok.get("token_id")
                        outcome = (tok.get("outcome") or "").strip()
                        if tid and outcome in ("Yes", "Up"):
                            token_map[tid] = (cond_to_idx[cid], outcome)

        all_token_ids = list(token_map.keys())

        async def poly_ws_stream():
            if not all_token_ids:
                return
            try:
                async with websockets.connect(MARKET_WS) as upstream:
                    await upstream.send(json.dumps({
                        "assets_ids": all_token_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }))
                    last_ping = time.time()
                    while not stop.is_set():
                        if time.time() - last_ping >= 9:
                            await upstream.send("PING")
                            last_ping = time.time()
                        try:
                            raw = await asyncio.wait_for(upstream.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        if raw == "PONG":
                            continue
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        updates = _extract_poly_prices(data, token_map, pairs)
                        for u in updates:
                            await ws.send_json(u)
            except WebSocketDisconnect:
                raise
            except Exception:
                pass

        await poly_ws_stream()

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stop.set()


def _extract_poly_prices(
    data: Any, token_map: Dict[str, tuple], pairs: List[Dict]
) -> List[Dict]:
    """Parse Polymarket WS message and return price update dicts."""
    updates = []

    def _handle_event(evt: Dict):
        if evt.get("event_type") not in ("best_bid_ask", "price_change"):
            return
        asset_id = evt.get("asset_id", "")
        info = token_map.get(asset_id)
        if not info:
            return
        best_bid = evt.get("best_bid")
        best_ask = evt.get("best_ask")
        if evt.get("price_changes"):
            pc = evt["price_changes"][0]
            best_bid = pc.get("best_bid", best_bid)
            best_ask = pc.get("best_ask", best_ask)
        if best_bid is None or best_ask is None:
            return
        pair_idx, _ = info
        try:
            mid = (float(best_bid) + float(best_ask)) / 2
            yes_pct = round(mid * 100, 1)
            old_pct = pairs[pair_idx].get("poly", {}).get("yes_pct")
            if yes_pct == old_pct:
                return
            pairs[pair_idx].setdefault("poly", {})["yes_pct"] = yes_pct
            updates.append({
                "type": "price",
                "index": pair_idx,
                "side": "poly",
                "yes_pct": yes_pct,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "ts": time.time(),
            })
        except (ValueError, TypeError):
            pass

    if isinstance(data, list):
        for item in data:
            _handle_event(item)
    elif isinstance(data, dict):
        _handle_event(data)
    return updates


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

_SYNONYMS = [
    ("democratic party", "democrats"), ("democrat party", "democrats"),
    ("republican party", "republicans"), ("gop", "republicans"),
    ("united states", "us"), ("u s ", "us "),
    ("control the senate", "win the senate"), ("control the house", "win the house"),
]

def _normalize_market(text: str) -> str:
    t = text.lower()
    for old, new in _SYNONYMS:
        t = t.replace(old, new)
    return _clean(t)

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

            poly_mkts_raw = pev.get("markets", [])
            kalshi_mkts_raw = best_kev.get("markets", [])

            def _mk_poly(m):
                return {
                    "question": m.get("question"),
                    "outcomes": m.get("outcomes"),
                    "outcomePrices": m.get("outcomePrices"),
                    "conditionId": m.get("conditionId"),
                    "endDate": m.get("endDate"),
                }

            def _mk_kalshi(m):
                return {
                    "title": m.get("title"),
                    "ticker": m.get("ticker"),
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": m.get("yes_ask"),
                    "no_bid": m.get("no_bid"),
                    "no_ask": m.get("no_ask"),
                    "volume": m.get("volume"),
                    "status": m.get("status"),
                    "expiration_time": m.get("expiration_time"),
                    "expected_expiration_time": m.get("expected_expiration_time"),
                }

            all_scores = []
            for pi, pm in enumerate(poly_mkts_raw):
                p_raw = _clean(pm.get("question", ""))
                p_norm = _normalize_market(pm.get("question", ""))
                for ki, km in enumerate(kalshi_mkts_raw):
                    k_raw = _clean(km.get("title", ""))
                    k_norm = _normalize_market(km.get("title", ""))
                    s_raw = max(
                        fuzz.token_set_ratio(p_raw, k_raw),
                        fuzz.token_sort_ratio(p_raw, k_raw),
                    )
                    s_norm = max(
                        fuzz.token_set_ratio(p_norm, k_norm),
                        fuzz.token_sort_ratio(p_norm, k_norm),
                    )
                    s_strict = fuzz.ratio(p_norm, k_norm)
                    s = max(s_raw, s_norm, int(s_strict * 0.9))
                    if s >= 55:
                        all_scores.append((s, pi, ki))
            all_scores.sort(key=lambda x: x[0], reverse=True)

            matched_pairs = []
            used_p, used_k = set(), set()
            for s, pi, ki in all_scores:
                if pi in used_p or ki in used_k:
                    continue
                used_p.add(pi)
                used_k.add(ki)
                matched_pairs.append({
                    "poly": _mk_poly(poly_mkts_raw[pi]),
                    "kalshi": _mk_kalshi(kalshi_mkts_raw[ki]),
                    "sub_score": round(s),
                })

            for pi, pm in enumerate(poly_mkts_raw):
                if pi not in used_p:
                    matched_pairs.append({
                        "poly": _mk_poly(pm),
                        "kalshi": None,
                        "sub_score": 0,
                    })

            for ki, km in enumerate(kalshi_mkts_raw):
                if ki not in used_k:
                    matched_pairs.append({
                        "poly": None,
                        "kalshi": _mk_kalshi(km),
                        "sub_score": 0,
                    })

            matched_pairs.sort(key=lambda x: x["sub_score"], reverse=True)

            matches.append({
                "score": round(best_score),
                "diff": diff,
                "poly": {
                    "title": pev.get("title"),
                    "slug": pev.get("slug"),
                    "yes_pct": poly_pct,
                    "volume": pev.get("volume"),
                    "liquidity": pev.get("liquidity"),
                    "endDate": pev.get("endDate"),
                    "markets_count": len(poly_mkts_raw),
                },
                "kalshi": {
                    "title": best_kev.get("title"),
                    "event_ticker": best_kev.get("event_ticker"),
                    "category": best_kev.get("category"),
                    "yes_pct": kalshi_pct,
                    "volume": sum(m.get("volume", 0) for m in kalshi_mkts_raw),
                    "endDate": kalshi_mkts_raw[0].get("expiration_time") if kalshi_mkts_raw else None,
                    "markets_count": len(kalshi_mkts_raw),
                },
                "matched_markets": matched_pairs,
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

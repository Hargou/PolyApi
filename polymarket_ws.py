"""
Polymarket WebSocket & Events Explorer
- Browse active events (grouped markets) via Gamma REST API
- Stream real-time orderbook/price data via Market WebSocket
- Stream live sports scores via Sports WebSocket
"""

import asyncio
import json
import sys
import time
import httpx
import websockets


MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
SPORTS_WS = "wss://sports-api.polymarket.com/ws"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


# ---------------------------------------------------------------------------
# REST: Events & Markets discovery
# ---------------------------------------------------------------------------

async def fetch_events(limit=10, order="volume_24hr"):
    """Fetch active events from the Gamma API. Each event groups related markets."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GAMMA_API}/events",
            params={
                "limit": limit,
                "active": "true",
                "closed": "false",
            },
        )
        resp.raise_for_status()
        events = resp.json()

    print(f"\n{'='*80}")
    print(f" TOP {len(events)} ACTIVE EVENTS  (by {order})")
    print(f"{'='*80}\n")

    for i, ev in enumerate(events, 1):
        title = ev.get("title", ev.get("slug", "N/A"))
        slug = ev.get("slug", "")
        volume = ev.get("volume", 0)
        liquidity = ev.get("liquidity", 0)
        markets = ev.get("markets", [])

        try:
            vol_str = f"${float(volume):,.0f}"
        except (ValueError, TypeError):
            vol_str = str(volume)
        try:
            liq_str = f"${float(liquidity):,.0f}"
        except (ValueError, TypeError):
            liq_str = str(liquidity)

        print(f"  {i:>2}. {title}")
        print(f"      slug: {slug}")
        print(f"      volume: {vol_str}   liquidity: {liq_str}   markets: {len(markets)}")

        for mk in markets[:6]:
            q = mk.get("question", "?")
            outcomes = mk.get("outcomes", "[]")
            prices = mk.get("outcomePrices", "[]")
            cid = mk.get("conditionId", "")[:30]
            print(f"        - {q[:70]}")
            print(f"          outcomes={outcomes}  prices={prices}")
        if len(markets) > 6:
            print(f"        ... +{len(markets) - 6} more markets")
        print()

    return events


async def fetch_tags():
    """List available category tags."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GAMMA_API}/tags")
        resp.raise_for_status()
        tags = resp.json()

    print(f"\n{'='*80}")
    print(f" AVAILABLE TAGS  ({len(tags)} total)")
    print(f"{'='*80}\n")

    for t in tags[:30]:
        print(f"  id={t.get('id','?'):<8}  label={t.get('label','?')}")
    if len(tags) > 30:
        print(f"  ... +{len(tags)-30} more")
    print()
    return tags


async def fetch_sports():
    """List sports metadata."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GAMMA_API}/sports")
        resp.raise_for_status()
        sports = resp.json()

    print(f"\n{'='*80}")
    print(f" SPORTS  ({len(sports)} leagues)")
    print(f"{'='*80}\n")

    for s in sports:
        label = s.get("label", "?")
        slug = s.get("slug", "?")
        tag_id = s.get("tagId", "?")
        print(f"  {label:<30}  slug={slug:<20}  tag_id={tag_id}")
    print()
    return sports


async def resolve_asset_ids(events, max_markets=5):
    """Given Gamma events, resolve CLOB token IDs for WebSocket subscription."""
    asset_ids = []
    market_map = {}  # asset_id -> question for pretty printing
    async with httpx.AsyncClient(timeout=15) as client:
        count = 0
        for ev in events:
            for mk in ev.get("markets", []):
                if count >= max_markets:
                    break
                cid = mk.get("conditionId", "")
                if not cid:
                    continue
                try:
                    r = await client.get(f"{CLOB_API}/markets/{cid}")
                    r.raise_for_status()
                    clob = r.json()
                except Exception:
                    continue
                if not clob.get("accepting_orders"):
                    continue
                for tok in clob.get("tokens", []):
                    tid = tok["token_id"]
                    asset_ids.append(tid)
                    market_map[tid] = f"{mk.get('question','?')} [{tok.get('outcome','?')}]"
                count += 1
            if count >= max_markets:
                break

    return asset_ids, market_map


# ---------------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------------

async def market_channel(asset_ids, market_map, duration=30):
    """Stream Market channel events."""
    print(f"\n{'='*80}")
    print(f" MARKET CHANNEL  -- streaming for {duration}s")
    print(f" Subscribed to {len(asset_ids)} token IDs across {len(asset_ids)//2} markets")
    print(f"{'='*80}\n")

    async with websockets.connect(MARKET_WS) as ws:
        sub = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(sub))

        end_time = time.time() + duration
        last_ping = time.time()
        msg_count = 0

        while time.time() < end_time:
            if time.time() - last_ping >= 9:
                await ws.send("PING")
                last_ping = time.time()

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue

            if raw == "PONG":
                continue

            msg_count += 1
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for item in data:
                        etype = item.get("event_type", "?")
                        aid = item.get("asset_id", "")
                        label = market_map.get(aid, aid[:40] + "...")
                        print(f"  [{msg_count:>4}] {etype:<20}  {label[:60]}")

                        if etype == "book":
                            bids = item.get("bids", [])
                            asks = item.get("asks", [])
                            print(f"         bids={len(bids)}  asks={len(asks)}")
                            if bids:
                                print(f"         best bid: {bids[0]}")
                            if asks:
                                print(f"         best ask: {asks[0]}")
                        elif etype == "price_change":
                            for ch in item.get("changes", [])[:2]:
                                print(f"         {ch}")
                        elif etype == "last_trade_price":
                            print(f"         price={item.get('price')}  side={item.get('side')}")
                        else:
                            print(f"         {json.dumps(item)[:300]}")
                        print()
                else:
                    etype = data.get("event_type", "?")
                    aid = data.get("asset_id", "")
                    label = market_map.get(aid, "")
                    if etype == "best_bid_ask":
                        print(f"  [{msg_count:>4}] {etype:<20}  {label[:50]}")
                        print(f"         bid={data.get('best_bid')}  ask={data.get('best_ask')}  spread={data.get('spread')}\n")
                    elif etype == "new_market":
                        print(f"  [{msg_count:>4}] NEW MARKET: {json.dumps(data)[:400]}\n")
                    elif etype == "market_resolved":
                        print(f"  [{msg_count:>4}] RESOLVED:   {json.dumps(data)[:400]}\n")
                    elif etype == "price_change":
                        changes = data.get("price_changes", [])
                        print(f"  [{msg_count:>4}] {etype:<20}  market={data.get('market','')[:30]}...")
                        for ch in changes[:2]:
                            a = ch.get("asset_id", "")
                            lbl = market_map.get(a, "")
                            print(f"         {lbl[:40]}  price={ch.get('price')}  size={ch.get('size')}  bid={ch.get('best_bid')} ask={ch.get('best_ask')}")
                        print()
                    else:
                        print(f"  [{msg_count:>4}] {json.dumps(data)[:400]}\n")
            except (json.JSONDecodeError, KeyError, IndexError):
                print(f"  [{msg_count:>4}] raw: {raw[:300]}\n")

    print(f"  >> Market channel total: {msg_count} messages\n")


async def sports_channel(duration=20):
    """Stream Sports channel events (no subscription needed)."""
    print(f"\n{'='*80}")
    print(f" SPORTS CHANNEL  -- streaming for {duration}s")
    print(f"{'='*80}\n")

    async with websockets.connect(SPORTS_WS) as ws:
        end_time = time.time() + duration
        msg_count = 0

        while time.time() < end_time:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue

            if raw == "ping":
                await ws.send("pong")
                continue

            msg_count += 1
            try:
                data = json.loads(raw)
                if "gameId" in data:
                    home = data.get("homeTeam", "?")
                    away = data.get("awayTeam", "?")
                    score = data.get("score", "?")
                    period = data.get("period", "?")
                    league = data.get("leagueAbbreviation", "?")
                    status = data.get("status", "?")
                    print(f"  [{msg_count:>4}] {league.upper():<5} {away} @ {home}  {score}  {period}  ({status})")
                else:
                    print(f"  [{msg_count:>4}] {json.dumps(data)[:300]}")
            except json.JSONDecodeError:
                print(f"  [{msg_count:>4}] raw: {raw[:300]}")
            print()

    print(f"  >> Sports channel total: {msg_count} messages\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """
Usage:  python polymarket_ws.py <command>

Commands:
  events      Show top active events (grouped markets) sorted by 24h volume
  tags        List all available category tags
  sports      List sports leagues & metadata
  stream      Stream real-time data from Market + Sports WebSocket channels
  all         Run everything: events -> tags -> stream

Examples:
  python polymarket_ws.py events
  python polymarket_ws.py stream
  python polymarket_ws.py all
"""


async def cmd_events():
    await fetch_events(limit=15)


async def cmd_tags():
    await fetch_tags()


async def cmd_sports_meta():
    await fetch_sports()


async def cmd_stream():
    events = await fetch_events(limit=10)
    asset_ids, market_map = await resolve_asset_ids(events, max_markets=8)
    if not asset_ids:
        print("  No subscribable markets found.")
        return
    print(f"  Resolved {len(asset_ids)} token IDs for streaming.\n")
    await asyncio.gather(
        market_channel(asset_ids, market_map, duration=30),
        sports_channel(duration=20),
    )


async def cmd_all():
    await fetch_tags()
    await fetch_sports()
    events = await fetch_events(limit=15)
    asset_ids, market_map = await resolve_asset_ids(events, max_markets=8)
    if not asset_ids:
        print("  No subscribable markets found.")
        return
    print(f"  Resolved {len(asset_ids)} token IDs for streaming.\n")
    await asyncio.gather(
        market_channel(asset_ids, market_map, duration=30),
        sports_channel(duration=20),
    )
    print("Done!")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    commands = {
        "events": cmd_events,
        "tags": cmd_tags,
        "sports": cmd_sports_meta,
        "stream": cmd_stream,
        "all": cmd_all,
    }
    if cmd in ("-h", "--help") or cmd not in commands:
        print(USAGE)
        return
    asyncio.run(commands[cmd]())


if __name__ == "__main__":
    main()

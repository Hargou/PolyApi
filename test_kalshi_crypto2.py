import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

with httpx.Client(timeout=30) as c:
    # Try various series tickers that might be 15-min BTC
    guesses = [
        "KXBTCU", "KXBTCUP", "KXBTCDOWN", "KXBTCUD",
        "KXBTC15", "KXBTC5", "KXBTC1H", "KXBTCM",
        "KXBTCP", "KXBTCF", "KXBTCS", "KXBTCR",
        "KXBTC15M", "KXETH", "KXETHD", "KXSOL",
        "KXBTCUD15", "KXBTCUPDOWN", "KXBTCSH",
        "KXBTCST", "KXBTCSHORT",
    ]
    for series in guesses:
        r = c.get(f"{BASE}/events", params={
            "status": "open",
            "series_ticker": series,
            "with_nested_markets": "true",
            "limit": 3,
        })
        evts = r.json().get("events", [])
        if evts:
            e = evts[0]
            m0 = e["markets"][0] if e.get("markets") else {}
            print(f"{series}: {len(evts)} events | {e['title'][:70]}")
            print(f"  ticker={e['event_ticker']} mkts={len(e.get('markets',[]))} exp={m0.get('expiration_time','')[:19]}")

    # Let's also look at all series tickers on page 2+3 of events to find crypto ones
    print("\n--- Scanning all events for crypto-related ones ---")
    cursor = ""
    crypto_events = []
    for page in range(5):
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = c.get(f"{BASE}/events", params=params)
        d = r.json()
        evts = d.get("events", [])
        cursor = d.get("cursor", "")
        if not evts:
            break
        for e in evts:
            t = (e.get("title","") + " " + e.get("event_ticker","")).lower()
            if any(kw in t for kw in ["btc","bitcoin","crypto","eth","ethereum","sol","solana","kxbtc","kxeth","kxsol"]):
                crypto_events.append(e)
        if not cursor:
            break

    print(f"Found {len(crypto_events)} crypto events total")
    for e in crypto_events[:20]:
        mkts = e.get("markets", [])
        m0 = mkts[0] if mkts else {}
        exp = m0.get("expiration_time", "")[:19]
        print(f"  {e['event_ticker']:30} | {e['title'][:60]} | {len(mkts)} mkts | exp={exp}")

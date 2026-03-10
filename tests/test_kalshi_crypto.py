import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

with httpx.Client(timeout=30) as c:
    # Try searching for BTC markets
    r = c.get(f"{BASE}/markets", params={
        "status": "active",
        "limit": 20,
    })
    data = r.json()
    markets = data.get("markets", [])
    btc_markets = [m for m in markets if "btc" in m.get("ticker", "").lower() or "bitcoin" in m.get("title", "").lower()]
    print(f"Found {len(btc_markets)} BTC markets in first page of {len(markets)} total")
    for m in btc_markets[:5]:
        print(f"  {m['ticker']}: {m['title'][:60]} | exp={m.get('expiration_time','')[:16]}")

    # Try series_ticker parameter
    print("\n--- Trying series_ticker filter ---")
    for series in ["KXBTC", "KXBTCD", "KXBTCUD", "KXBTCUP", "KXBTCDOWN", "KXBTCP"]:
        r2 = c.get(f"{BASE}/events", params={
            "status": "open",
            "series_ticker": series,
            "with_nested_markets": "true",
            "limit": 5,
        })
        evts = r2.json().get("events", [])
        if evts:
            print(f"  {series}: {len(evts)} events")
            for e in evts[:2]:
                print(f"    {e['event_ticker']}: {e['title'][:60]} ({len(e.get('markets',[]))} mkts)")
                if e.get("markets"):
                    m0 = e["markets"][0]
                    print(f"      ticker={m0['ticker']} exp={m0.get('expiration_time','')[:16]} yes_bid={m0.get('yes_bid')} yes_ask={m0.get('yes_ask')}")

    # Also try searching by event title keyword
    print("\n--- Searching for crypto events ---")
    for cursor_str in ["", ]:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        r3 = c.get(f"{BASE}/events", params=params)
        d3 = r3.json()
        all_evts = d3.get("events", [])
        crypto = [e for e in all_evts if any(kw in e.get("title","").lower() for kw in ["btc","bitcoin","crypto","eth","ethereum"])]
        print(f"  Page 1: {len(crypto)} crypto events out of {len(all_evts)}")
        for e in crypto[:10]:
            print(f"    {e['event_ticker']}: {e['title'][:70]} ({len(e.get('markets',[]))} mkts)")
            if e.get("markets"):
                m0 = e["markets"][0]
                print(f"      ticker={m0['ticker']} exp={m0.get('expiration_time','')[:19]}")

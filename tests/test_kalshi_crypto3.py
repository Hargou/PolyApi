import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

with httpx.Client(timeout=30) as c:
    # Look at KXBTC series more carefully - get all events
    for series in ["KXBTC", "KXBTCD", "KXETHD", "KXETH"]:
        r = c.get(f"{BASE}/events", params={
            "status": "open",
            "series_ticker": series,
            "with_nested_markets": "true",
            "limit": 20,
        })
        evts = r.json().get("events", [])
        print(f"\n=== {series}: {len(evts)} events ===")
        for e in evts:
            mkts = e.get("markets", [])
            print(f"  {e['event_ticker']}: {e['title'][:65]} ({len(mkts)} mkts)")
            for m in mkts[:3]:
                exp = m.get("expiration_time", "")[:19]
                print(f"    {m['ticker']}: {m['title'][:50]} | yes_bid={m.get('yes_bid')} ask={m.get('yes_ask')} exp={exp}")

    # Try to find 15min markets - maybe different series
    print("\n=== Searching markets endpoint for 15min crypto ===")
    for series in ["KXBTC-15M", "KXBTCUD15", "KXBTC15", "KXBTCSHORT", "KXBTCM15", "KXBTCMIN",
                    "KXBTCPRICE15", "KXBTCRANGE15", "KXBTCFIFTEEN", "KXBTCQ", "KXBTCQUICK"]:
        r = c.get(f"{BASE}/events", params={
            "status": "open", "series_ticker": series,
            "with_nested_markets": "true", "limit": 3,
        })
        evts = r.json().get("events", [])
        if evts:
            print(f"  {series}: {len(evts)} events -> {evts[0]['title'][:60]}")

    # Try the markets endpoint with a filter
    print("\n=== Trying markets endpoint directly ===")
    r = c.get(f"{BASE}/markets", params={"limit": 200, "status": "active"})
    d = r.json()
    mkts = d.get("markets", [])
    print(f"Total markets: {len(mkts)}")
    btc_mkts = [m for m in mkts if "btc" in m.get("ticker","").lower() or "bitcoin" in m.get("title","").lower()]
    print(f"BTC markets: {len(btc_mkts)}")
    for m in btc_mkts[:10]:
        exp = m.get("expiration_time", "")[:19]
        print(f"  {m['ticker']:40} | {m['title'][:50]} | exp={exp}")

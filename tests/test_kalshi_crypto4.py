import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

with httpx.Client(timeout=30) as c:
    # Try all possible BTC series variations for short-duration
    short_series = [
        "KXBTC15M", "KXBTC5M", "KXBTC1H", "KXBTC30M",
        "KXBTCUP15", "KXBTCDOWN15", "KXBTCUD15",
        "KXBTCINTRA", "KXBTCMIN",
        "KXBTCPRICE", "KXBTCFLASH",
        "KXBTCSPOT", "KXBTCNOW",
        "KXBTCRNG", "KXBTCRANGE",
        "INTRABTC", "BTC15", "BTCUD",
        "KXBTCUD", "KXBTCUPDOWN",
        "CX-BTC-15", "CXBTC",
        # Hourly, 4H, etc
        "KXBTC1H", "KXBTC4H", "KXBTC1D",
        "KXBTCH", "KXBTCHR",
    ]
    print("--- Brute force series search ---")
    for series in short_series:
        r = c.get(f"{BASE}/events", params={
            "status": "open", "series_ticker": series,
            "with_nested_markets": "true", "limit": 2,
        })
        evts = r.json().get("events", [])
        if evts:
            print(f"  HIT: {series}: {len(evts)} events -> {evts[0]['title'][:70]}")

    # Try the series endpoint if it exists
    print("\n--- Trying /series endpoint ---")
    try:
        r = c.get(f"{BASE}/series")
        if r.status_code == 200:
            data = r.json()
            series_list = data.get("series", data if isinstance(data, list) else [])
            crypto_series = [s for s in series_list if isinstance(s, dict) and
                any(kw in json.dumps(s).lower() for kw in ["btc","bitcoin","crypto","eth"])]
            print(f"Found {len(crypto_series)} crypto series out of {len(series_list)} total")
            for s in crypto_series[:20]:
                print(f"  {s.get('ticker','?')}: {s.get('title','?')[:60]}")
        else:
            print(f"  Status: {r.status_code}")
    except Exception as e:
        print(f"  Error: {e}")

    # Check /series/KXBTC
    print("\n--- /series/KXBTC ---")
    try:
        r = c.get(f"{BASE}/series/KXBTC")
        if r.status_code == 200:
            d = r.json()
            print(json.dumps(d, indent=2)[:1000])
    except Exception as e:
        print(f"  Error: {e}")

import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

with httpx.Client(timeout=30) as c:
    # Check all short-duration crypto series
    short_series = [
        "KXBTC15M", "KXBTC5M", "KXBTC1H", "KXBTC30M",
        "KXETH15M", "KXETH5M", "KXETH1H", "KXETH30M",
        "KXSOL15M", "KXSOL5M", "KXSOL1H", "KXSOL30M",
    ]
    for series in short_series:
        r = c.get(f"{BASE}/events", params={
            "status": "open", "series_ticker": series,
            "with_nested_markets": "true", "limit": 5,
        })
        evts = r.json().get("events", [])
        if evts:
            print(f"\n=== {series}: {len(evts)} events ===")
            for e in evts[:3]:
                mkts = e.get("markets", [])
                print(f"  {e['event_ticker']}: {e['title'][:70]} ({len(mkts)} mkts)")
                for m in mkts[:3]:
                    exp = m.get("expiration_time", "")[:19]
                    print(f"    {m['ticker']}: {m.get('title','')[:55]} | yes_bid={m.get('yes_bid')} ask={m.get('yes_ask')} exp={exp}")

    # Also check the series metadata for KXBTC15M
    print("\n=== Series details: KXBTC15M ===")
    r = c.get(f"{BASE}/series/KXBTC15M")
    if r.status_code == 200:
        d = r.json().get("series", {})
        print(f"  category: {d.get('category')}")
        print(f"  frequency: {d.get('frequency')}")
        print(f"  title: {d.get('title')}")
        print(f"  settlement_sources: {json.dumps(d.get('settlement_sources',''))[:200]}")

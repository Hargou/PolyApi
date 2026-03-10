import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://api.elections.kalshi.com/trade-api/v2"

with httpx.Client(timeout=30) as c:
    for series in ["KXBTC15M", "KXETH15M", "KXSOL15M"]:
        r = c.get(f"{BASE}/events", params={
            "status": "open", "series_ticker": series,
            "with_nested_markets": "true", "limit": 3,
        })
        evts = r.json().get("events", [])
        for e in evts:
            print(f"\n=== {e['event_ticker']}: {e['title']} ===")
            for m in e.get("markets", []):
                print(f"  ticker: {m['ticker']}")
                print(f"  title: {m['title']}")
                print(f"  close_time: {m.get('close_time')}")
                print(f"  open_time: {m.get('open_time')}")
                print(f"  expiration_time: {m.get('expiration_time')}")
                print(f"  expected_expiration_time: {m.get('expected_expiration_time')}")
                print(f"  status: {m.get('status')}")

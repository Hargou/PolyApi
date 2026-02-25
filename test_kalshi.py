"""Quick test: find overlapping events between Kalshi and Polymarket."""
import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

def fetch_all_kalshi_events():
    events = []
    cursor = ""
    while len(events) < 800:
        url = f"{KALSHI}/events?status=open&limit=200&with_nested_markets=true"
        if cursor:
            url += f"&cursor={cursor}"
        r = httpx.get(url, timeout=20)
        data = r.json()
        batch = data.get("events", [])
        if not batch:
            break
        events.extend(batch)
        cursor = data.get("cursor", "")
        if not cursor:
            break
    return events


def main():
    print("Fetching Kalshi events...")
    kalshi = fetch_all_kalshi_events()
    print(f"Got {len(kalshi)} Kalshi events\n")

    topics = {
        "gta": ["gta"],
        "taylor swift": ["taylor swift"],
        "trump": ["trump"],
        "uk pm/election": ["prime minister of the uk", "uk election", "next uk general"],
        "pope": ["pope"],
        "ukraine/russia": ["ukraine", "ceasefire"],
        "xi jinping": ["xi jinping"],
        "macron": ["macron"],
        "starmer": ["starmer"],
        "putin": ["putin"],
        "nato": ["nato"],
        "israel/iran": ["israel", "iran"],
        "openai": ["openai"],
        "spacex": ["spacex"],
        "bitcoin/crypto": ["bitcoin", "crypto"],
        "deport": ["deport"],
        "mars": ["mars"],
    }

    print("=" * 100)
    print(f"{'TOPIC':20} {'KALSHI TITLE':55} {'YES BID':>8} {'YES ASK':>8} {'VOLUME':>10}")
    print("=" * 100)

    for topic, kws in topics.items():
        matches = [
            e for e in kalshi
            if any(kw in e.get("title", "").lower() for kw in kws)
        ]
        if not matches:
            print(f"{topic:20} -- no match on Kalshi --")
            continue
        for m in matches[:3]:
            mkts = m.get("markets", [])
            m0 = mkts[0] if mkts else {}
            yb = m0.get("yes_bid", 0)
            ya = m0.get("yes_ask", 0)
            vol = sum(x.get("volume", 0) for x in mkts)
            title = m.get("title", "?")[:55]
            print(f"{topic:20} {title:55} {yb:>8} {ya:>8} {vol:>10}")

    # Now fetch Polymarket events and try to match
    print("\n\nFetching Polymarket events...")
    poly = httpx.get("http://127.0.0.1:8000/api/events?limit=50&offset=0", timeout=15).json()
    print(f"Got {len(poly)} Polymarket events\n")

    # Simple fuzzy matching by common keywords
    match_kws = [
        "gta", "trump", "taylor swift", "pope", "ukraine", "ceasefire",
        "putin", "macron", "starmer", "nato", "xi jinping", "spacex",
        "openai", "deport", "bitcoin", "mars", "uk election", "israel", "iran",
    ]

    print("=" * 120)
    print(f"{'KEYWORD':15} {'POLYMARKET':45} {'POLY %':>7} {'KALSHI':45} {'KALSHI %':>8}")
    print("=" * 120)

    for kw in match_kws:
        poly_match = [e for e in poly if kw in (e.get("title", "") + " " + e.get("slug", "")).lower()]
        kalshi_match = [e for e in kalshi if kw in e.get("title", "").lower()]

        if not poly_match and not kalshi_match:
            continue

        pm = poly_match[0] if poly_match else None
        km = kalshi_match[0] if kalshi_match else None

        pm_title = pm.get("title", "")[:43] if pm else "---"
        km_title = km.get("title", "")[:43] if km else "---"

        # Get Polymarket yes price from first market
        pm_pct = "---"
        if pm and pm.get("markets"):
            try:
                prices = json.loads(pm["markets"][0].get("outcomePrices", "[]"))
                if prices:
                    pm_pct = f"{float(prices[0])*100:.1f}%"
            except:
                pass

        # Get Kalshi yes price
        km_pct = "---"
        if km and km.get("markets"):
            yb = km["markets"][0].get("yes_bid", 0)
            ya = km["markets"][0].get("yes_ask", 0)
            mid = (yb + ya) / 2 if ya else yb
            km_pct = f"{mid:.0f}%"

        print(f"{kw:15} {pm_title:45} {pm_pct:>7} {km_title:45} {km_pct:>8}")


if __name__ == "__main__":
    main()

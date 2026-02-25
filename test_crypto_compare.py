import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

r = httpx.get("http://127.0.0.1:8000/api/live-crypto-compare", timeout=30)
data = r.json()
print(f"Total pairs: {len(data)}\n")
for p in data:
    poly = p.get("poly")
    kalshi = p.get("kalshi")
    gap = p.get("gap")
    ws = p.get("window_start", "")[:19]
    we = p.get("window_end", "")[:19]
    matched = p.get("matched")
    print(f"[{p['asset']}] {ws} -> {we} | matched={matched} gap={gap}")
    if poly:
        print(f"  POLY:   {poly['title'][:55]} | yes={poly['yes_pct']}%")
    else:
        print(f"  POLY:   (none)")
    if kalshi:
        print(f"  KALSHI: {kalshi['title'][:55]} | yes={kalshi['yes_pct']}%")
    else:
        print(f"  KALSHI: (none)")
    print()

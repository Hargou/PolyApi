import httpx, sys
sys.stdout.reconfigure(encoding="utf-8")
r = httpx.get("http://127.0.0.1:8000/api/compare", timeout=60)
d = r.json()
print(f"Total event matches: {len(d)}\n")
for p in d[:5]:
    pt = p["poly"]["title"][:55]
    kt = p["kalshi"]["title"][:55]
    print(f"=== [{p['score']}%] {pt} vs {kt} ===")
    mm = p.get("matched_markets", [])
    paired = [m for m in mm if m["sub_score"] > 0]
    print(f"  {len(paired)} paired / {len(mm)} total sub-markets")
    for m in mm[:8]:
        pq = m["poly"]["question"][:42] if m["poly"] else "(no poly)"
        ktt = m["kalshi"]["title"][:42] if m["kalshi"] else "(no kalshi)"
        tag = f"{m['sub_score']}%" if m["sub_score"] > 0 else "UNMATCHED"
        print(f"  {tag:>10} | P: {pq:42} | K: {ktt}")
    print()

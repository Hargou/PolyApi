import httpx, json, sys
sys.stdout.reconfigure(encoding="utf-8")

r = httpx.get("http://127.0.0.1:8000/api/compare", timeout=60)
print("Status:", r.status_code)
data = r.json()
print(f"Matches found: {len(data)}\n")

print(f"{'SCORE':>5} {'GAP':>8} | {'POLYMARKET':50} {'POLY%':>6} | {'KALSHI':50} {'KAL%':>6}")
print("-" * 135)

for p in data:
    score = p["score"]
    diff = p["diff"]
    pt = p["poly"]["title"][:48]
    pp = p["poly"]["yes_pct"]
    kt = p["kalshi"]["title"][:48]
    kp = p["kalshi"]["yes_pct"]
    diff_str = f"{diff:.1f}" if diff is not None else "---"
    pp_str = f"{pp}" if pp is not None else "---"
    kp_str = f"{kp}" if kp is not None else "---"
    print(f"{score:>5} {diff_str:>8} | {pt:50} {pp_str:>6} | {kt:50} {kp_str:>6}")

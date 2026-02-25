"""Check what the CLOB API returns for token data."""
import httpx
import json

BASE = "http://127.0.0.1:8000"

# Get a conditionId from the crypto compare
r = httpx.get(f"{BASE}/api/live-crypto-compare", timeout=20)
pairs = r.json()
for p in pairs[:3]:
    if not p.get("poly") or not p["poly"].get("conditionId"):
        continue
    cid = p["poly"]["conditionId"]
    print(f"\n--- {p['asset']} conditionId={cid[:30]}... ---")
    cr = httpx.get(f"{BASE}/api/clob/{cid}", timeout=15)
    mkt = cr.json()
    tokens = mkt.get("tokens", [])
    print(f"  {len(tokens)} tokens")
    for t in tokens:
        print(f"  outcome={t.get('outcome')!r}  token_id={t.get('token_id', '?')[:40]}...")
        print(f"    all keys: {list(t.keys())}")

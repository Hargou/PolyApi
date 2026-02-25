"""Diagnose crypto arb flow: REST, WebSocket, timing."""
import asyncio
import time
import httpx
import websockets
import json

BASE = "http://127.0.0.1:8000"
WS_URI = "ws://127.0.0.1:8000/ws/crypto-arb"


def test_rest_live_crypto():
    print("1. GET /api/live-crypto ...")
    t0 = time.time()
    r = httpx.get(f"{BASE}/api/live-crypto", timeout=30)
    elapsed = time.time() - t0
    print(f"   {r.status_code} in {elapsed:.1f}s, {len(r.json())} events")


def test_rest_crypto_compare():
    print("\n2. GET /api/live-crypto-compare ...")
    t0 = time.time()
    r = httpx.get(f"{BASE}/api/live-crypto-compare", timeout=30)
    elapsed = time.time() - t0
    d = r.json()
    print(f"   {r.status_code} in {elapsed:.1f}s, {len(d)} markets")
    if d:
        print(f"   First: {d[0]['asset']} {d[0].get('window_min')}m {d[0]['poly']['yes_pct']}%")


async def test_ws():
    print("\n3. WebSocket /ws/crypto-arb ...")
    t0 = time.time()
    try:
        async with websockets.connect(WS_URI, open_timeout=15, close_timeout=5) as ws:
            print(f"   Connected in {time.time()-t0:.1f}s")
            got_connected = False
            got_snapshot = False
            price_count = 0
            deadline = time.time() + 12
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    msg = json.loads(raw)
                    if msg.get("type") == "connected":
                        got_connected = True
                        print(f"   Got 'connected' at {time.time()-t0:.1f}s")
                    if msg.get("type") == "snapshot":
                        got_snapshot = True
                        n = len(msg.get("pairs", []))
                        print(f"   Snapshot at {time.time()-t0:.1f}s: {n} markets")
                    elif msg.get("type") == "price":
                        price_count += 1
                except asyncio.TimeoutError:
                    break
            print(f"   After 12s: connected={got_connected}, snapshot={got_snapshot}, price_updates={price_count}")
    except Exception as e:
        print(f"   ERROR: {e}")
    print(f"   Total: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    test_rest_live_crypto()
    test_rest_crypto_compare()
    asyncio.run(test_ws())
    print("\nDone.")

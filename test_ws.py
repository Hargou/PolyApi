"""Test Polymarket crypto WebSocket (5m, 10m, 15m)."""
import asyncio
import json
import websockets
import time

WS_URI = "ws://127.0.0.1:8000/ws/crypto-arb"


async def test():
    print(f"Connecting to {WS_URI} ...")
    async with websockets.connect(WS_URI, open_timeout=15) as ws:
        print("Connected. Waiting 20s for messages...\n")
        start = time.time()
        count = 0
        while time.time() - start < 20:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                msg = json.loads(raw)
                count += 1
                t = msg.get("type", "?")
                if t == "snapshot":
                    pairs = msg.get("pairs", [])
                    print(f"[{count}] SNAPSHOT — {len(pairs)} markets")
                    for p in pairs[:6]:
                        wm = p.get("window_min", "?")
                        print(f"     {p['asset']} {wm}m  {p['poly']['yes_pct']}%  {p['window_end'][:19]}")
                elif t == "price":
                    idx, side, pct = msg.get("index"), msg.get("side"), msg.get("yes_pct")
                    print(f"[{count}] PRICE idx={idx} {side}={pct}%")
            except asyncio.TimeoutError:
                continue
        print(f"\nDone — {count} messages in 20s")


if __name__ == "__main__":
    asyncio.run(test())

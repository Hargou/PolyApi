"""
Test Kraken WebSocket for spot prices - debug slowness and timeout.
Run: python test_kraken_spot.py
"""
import asyncio
import json
import time
import websockets

KRAKEN_WS = "wss://ws.kraken.com/v2"

async def test_kraken(duration=120):
    print("Connecting to Kraken...")
    msg_count = 0
    last_msg = 0
    gaps = []
    
    async with websockets.connect(KRAKEN_WS) as ws:
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": ["BTC/USD"], "event_trigger": "trades"}
        }))
        print("Subscribed to BTC/USD (trades). Running for", duration, "s...\n")
        start = time.time()
        
        async def ping_task():
            while True:
                await asyncio.sleep(25)
                try:
                    await ws.send(json.dumps({"method": "ping"}))
                    print("  [PING sent]")
                except Exception as e:
                    print("  [PING failed]", e)
                    break
        
        ping = asyncio.create_task(ping_task())
        
        try:
            while time.time() - start < duration:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=35)
                except asyncio.TimeoutError:
                    print("\n*** TIMEOUT after 35s - no message received ***")
                    break
                except Exception as e:
                    print("\n*** ERROR:", e)
                    break
                    
                msg_count += 1
                now = time.time()
                gap = now - last_msg if last_msg else 0
                if last_msg and gap > 2:
                    gaps.append((msg_count, gap))
                last_msg = now
                
                try:
                    data = json.loads(raw)
                    if data.get("channel") == "ticker" and data.get("data"):
                        d = data["data"][0] if isinstance(data["data"], list) else data["data"]
                        bid, ask = d.get("bid"), d.get("ask")
                        last = d.get("last")
                        mid = (float(bid) + float(ask)) / 2 if bid and ask else last
                        ts = data.get("timestamp", "")
                        print(f"  [{msg_count:4d}] {mid:.2f}  gap={gap:.2f}s  ts={ts[:19] if ts else '-'}")
                    elif data.get("method") == "pong":
                        print("  [PONG received]")
                    elif data.get("channel") == "heartbeat":
                        print("  [heartbeat]")
                except Exception as e:
                    print("  [parse err]", str(e)[:50])
        finally:
            ping.cancel()
            try:
                await ping
            except asyncio.CancelledError:
                pass
    
    elapsed = time.time() - start
    rate = msg_count / elapsed if elapsed > 0 else 0
    print(f"\n--- Summary ---")
    print(f"Messages: {msg_count} in {elapsed:.1f}s = {rate:.2f}/s")
    if gaps:
        print(f"Gaps >2s: {len(gaps)}", gaps[:5])
    print("Done.")

if __name__ == "__main__":
    asyncio.run(test_kraken(120))

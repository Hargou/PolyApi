"""
Test all spot price WebSocket sources - find which works and how fast.
Run: python test_spot_sources.py

Typical results (US):
- Binance: HTTP 451 (geo-blocked)
- Polymarket RTDS: HTTP 429 (rate limited)
- Kraken: ~0.3-1/s (slow)
- Coinbase: ~18/s (FAST - best free option)
- Bybit: ~2/s (decent fallback)
"""
import asyncio
import json
import time
import sys

try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

async def test_binance_us(duration=15):
    """Binance US - for US users, not geo-blocked."""
    url = "wss://stream.binance.us:9443/stream?streams=btcusdt@ticker"
    print("\n[1] BINANCE US", url)
    try:
        async with websockets.connect(url, close_timeout=5) as ws:
            count = 0
            start = time.time()
            while time.time() - start < duration:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                count += 1
                d = json.loads(raw)
                data = d.get("data", d)
                p = float(data.get("c", data.get("p", 0)))
                if count <= 3 or count % 20 == 0:
                    print(f"    #{count} ${p:,.2f}")
            rate = count / duration
            print(f"    OK: {count} msgs in {duration}s = {rate:.1f}/s")
            return rate
    except Exception as e:
        print(f"    FAIL: {e}")
        return 0

async def test_binance(duration=15):
    """Binance (global) - fastest, free. May be geo-blocked in US."""
    url = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    print("\n[2] BINANCE (global)", url)
    try:
        async with websockets.connect(url, close_timeout=5) as ws:
            count = 0
            start = time.time()
            while time.time() - start < duration:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                count += 1
                if count <= 3 or count % 20 == 0:
                    d = json.loads(raw)
                    p = float(d.get("c", d.get("p", 0)))
                    print(f"    #{count} ${p:,.2f}")
            rate = count / duration
            print(f"    OK: {count} msgs in {duration}s = {rate:.1f}/s")
            return rate
    except Exception as e:
        print(f"    FAIL: {e}")
        return 0

async def test_polymarket_rtds(duration=15):
    """Polymarket RTDS - Binance feed via Polymarket. No geo-block."""
    url = "wss://ws-live-data.polymarket.com"
    print("\n[3] POLYMARKET RTDS", url)
    try:
        async with websockets.connect(url, close_timeout=10) as ws:
            await ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [{"topic": "crypto_prices", "type": "update", "filters": "btcusdt"}]
            }))
            count = 0
            start = time.time()
            ping_task = asyncio.create_task(asyncio.sleep(0))
            while time.time() - start < duration:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                if raw == "PONG":
                    continue
                count += 1
                try:
                    d = json.loads(raw)
                    p = d.get("payload", {})
                    if p.get("symbol") == "btcusdt" and "value" in p:
                        if count <= 3 or count % 20 == 0:
                            print(f"    #{count} ${p['value']:,.2f}")
                except: pass
            rate = count / duration
            print(f"    OK: {count} msgs in {duration}s = {rate:.1f}/s")
            return rate
    except Exception as e:
        print(f"    FAIL: {e}")
        return 0

async def test_kraken(duration=15):
    """Kraken - works globally, ~1/s."""
    url = "wss://ws.kraken.com/v2"
    print("\n[4] KRAKEN", url)
    try:
        async with websockets.connect(url, close_timeout=10) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "params": {"channel": "ticker", "symbol": ["BTC/USD"], "event_trigger": "trades"}
            }))
            count = 0
            start = time.time()
            while time.time() - start < duration:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                try:
                    d = json.loads(raw)
                    if d.get("channel") == "ticker" and d.get("data"):
                        count += 1
                        arr = d["data"] if isinstance(d["data"], list) else [d["data"]]
                        bid = arr[0].get("bid") or 0
                        ask = arr[0].get("ask") or 0
                        mid = (float(bid) + float(ask)) / 2 if bid and ask else 0
                        if count <= 3 or count % 5 == 0:
                            print(f"    #{count} ${mid:,.2f}")
                except: pass
            rate = count / duration
            print(f"    OK: {count} msgs in {duration}s = {rate:.1f}/s")
            return rate
    except Exception as e:
        print(f"    FAIL: {e}")
        return 0

async def test_coinbase(duration=15):
    """Coinbase - free, US-based."""
    url = "wss://ws-feed.exchange.coinbase.com"
    print("\n[5] COINBASE", url)
    try:
        async with websockets.connect(url, close_timeout=10) as ws:
            await ws.send(json.dumps({
                "type": "subscribe",
                "product_ids": ["BTC-USD"],
                "channels": ["ticker"]
            }))
            count = 0
            start = time.time()
            while time.time() - start < duration:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                d = json.loads(raw)
                if d.get("type") == "ticker" and "price" in d:
                    count += 1
                    if count <= 3 or count % 20 == 0:
                        print(f"    #{count} ${float(d['price']):,.2f}")
            rate = count / duration
            print(f"    OK: {count} msgs in {duration}s = {rate:.1f}/s")
            return rate
    except Exception as e:
        print(f"    FAIL: {e}")
        return 0

async def test_bybit(duration=15):
    """Bybit - free, no auth."""
    url = "wss://stream.bybit.com/v5/public/spot"
    print("\n[6] BYBIT", url)
    try:
        async with websockets.connect(url, close_timeout=10) as ws:
            await ws.send(json.dumps({
                "op": "subscribe",
                "args": ["tickers.BTCUSDT"]
            }))
            count = 0
            start = time.time()
            while time.time() - start < duration:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                d = json.loads(raw)
                if d.get("topic") == "tickers.BTCUSDT" and "data" in d:
                    count += 1
                    p = float(d["data"].get("lastPrice", 0))
                    if count <= 3 or count % 20 == 0:
                        print(f"    #{count} ${p:,.2f}")
            rate = count / duration
            print(f"    OK: {count} msgs in {duration}s = {rate:.1f}/s")
            return rate
    except Exception as e:
        print(f"    FAIL: {e}")
        return 0

async def main():
    print("=" * 60)
    print("SPOT PRICE WEBSOCKET SOURCE TEST (15s each)")
    print("=" * 60)
    results = {}
    for name, fn in [
        ("Binance US", test_binance_us),
        ("Binance (global)", test_binance),
        ("Polymarket RTDS", test_polymarket_rtds),
        ("Kraken", test_kraken),
        ("Coinbase", test_coinbase),
        ("Bybit", test_bybit),
    ]:
        try:
            r = await fn(15)
            results[name] = r
        except Exception as e:
            print(f"  Error: {e}")
            results[name] = 0
    print("\n" + "=" * 60)
    print("SUMMARY (msgs/sec)")
    for name, rate in sorted(results.items(), key=lambda x: -x[1]):
        status = "FAST" if rate > 2 else "OK" if rate > 0 else "FAIL"
        print(f"  {name}: {rate:.1f}/s  [{status}]")
    best = max(results, key=results.get)
    print(f"\nBest: {best} ({results[best]:.1f}/s)")

if __name__ == "__main__":
    asyncio.run(main())

"""
Test Kraken WebSocket for spot prices.
"""
import asyncio
import json
import websockets

KRAKEN_WS = "wss://ws.kraken.com/v2"
PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD"]


async def test():
    print("Connecting to Kraken...", flush=True)
    async with websockets.connect(KRAKEN_WS) as ws:
        print("Subscribing to ticker...", flush=True)
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": PAIRS}
        }))
        print("Waiting for messages...", flush=True)
        for i in range(15):
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get("channel") == "ticker" and data.get("data"):
                for d in (data["data"] if isinstance(data["data"], list) else [data["data"]]):
                    sym = d.get("symbol", "?")
                    last = d.get("last", "?")
                    print(f"  [{i+1}] {sym} = {last}", flush=True)
            else:
                print(f"  [{i+1}] {str(data)[:80]}...", flush=True)
    print("Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(test())

"""
Polymarket Crypto 5-Min Prediction Dashboard — FastAPI backend
Server-side engines for spot prices, market discovery, and CLOB streaming.
Single /ws/feed endpoint pushes unified stream to browser.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from engines.price_engine import PriceEngine
from engines.market_engine import MarketEngine
from engines.feed import Feed
from engines.paper_session import PaperSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CLOB = "https://clob.polymarket.com"

# -- Shared instances --
feed = Feed()
price_engine = PriceEngine()
market_engine = MarketEngine()
paper_session = PaperSession()

# Track background tasks to avoid fire-and-forget warnings
_bg_tasks: set[asyncio.Task] = set()


def _create_bg_task(coro):
    """Create a tracked background task that cleans up after itself."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# -- Engine callbacks wired to feed --

def on_price(sym: str, value: float, ts: int):
    """Called by PriceEngine on each spot price tick."""
    feed.update_snapshot_prices(price_engine.prices)
    _create_bg_task(feed.broadcast({
        "type": "price",
        "symbol": sym,
        "value": value,
        "ts": ts,
    }))


def on_market_event(data):
    """Called by MarketEngine for each CLOB WebSocket message."""
    _create_bg_task(feed.broadcast({
        "type": "clob",
        "data": data,
    }))


def on_markets_update(markets: list):
    """Called by MarketEngine when the market list is refreshed."""
    feed.update_snapshot_markets(markets)
    _create_bg_task(feed.broadcast({
        "type": "markets_update",
        "markets": markets,
    }))


# -- Wire callbacks --
price_engine._on_price = on_price
market_engine._on_market_event = on_market_event
market_engine._on_markets_update = on_markets_update


# -- App lifespan --

@asynccontextmanager
async def lifespan(app: FastAPI):
    await price_engine.start()
    await market_engine.start()
    log.info("All engines started")
    yield
    await price_engine.stop()
    await market_engine.stop()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
    log.info("All engines stopped")


app = FastAPI(title="Polymarket Crypto 5-Min Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# -- Routes --

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/paper", response_class=HTMLResponse)
async def paper():
    """Strategy Lab — paper trading & backtesting placeholder."""
    with open("static/paper.html") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.websocket("/ws/feed")
async def ws_feed(ws: WebSocket):
    """Single browser WebSocket — receives unified stream from all engines."""
    await feed.connect(ws)
    try:
        while True:
            # Keep connection alive; ignore any client messages
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        feed.disconnect(ws)


# -- Debug REST endpoints (read from engine state) --

@app.get("/api/markets-5m")
async def get_markets_5m():
    """Return current discovered markets from engine state."""
    return market_engine.markets


@app.get("/api/spot-prices")
async def get_spot_prices():
    """Return latest spot prices from engine state."""
    return price_engine.prices


@app.get("/api/clob/{condition_id}")
async def get_clob_market(condition_id: str):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{CLOB}/markets/{condition_id}")
        r.raise_for_status()
        return r.json()


# -- Paper Trading endpoints --

@app.websocket("/ws/paper")
async def ws_paper(ws: WebSocket):
    """WebSocket for live paper trading updates."""
    await paper_session.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        paper_session.disconnect(ws)


@app.post("/api/paper/start")
async def start_paper(strategy: str = "spot_momentum", bankroll: float = 10000.0,
                      book_poll: float = 10.0):
    """Start a paper trading session."""
    result = await paper_session.start(
        strategy_name=strategy,
        bankroll=bankroll,
        book_poll=book_poll,
        price_engine=price_engine,
        market_engine=market_engine,
    )
    return result


@app.post("/api/paper/stop")
async def stop_paper():
    """Stop the paper trading session."""
    await paper_session.stop()
    return {"status": "stopped"}


@app.get("/api/paper/state")
async def paper_state():
    """Get current paper trading state."""
    return paper_session._build_snapshot()

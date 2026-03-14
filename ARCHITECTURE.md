# Polymarket Crypto 5-Min Terminal — Architecture

## Table of Contents

1. [Project Layout](#project-layout)
2. [How to Run](#how-to-run)
3. [Backend Architecture](#backend-architecture)
   - [Application Entry Point (`app.py`)](#application-entry-point-apppy)
   - [PriceEngine (`engines/price_engine.py`)](#priceengine-enginesprice_enginepy)
   - [MarketEngine (`engines/market_engine.py`)](#marketengine-enginesmarket_enginepy)
   - [Feed (`engines/feed.py`)](#feed-enginesfeedpy)
4. [Data Flow](#data-flow)
5. [WebSocket Protocol](#websocket-protocol)
6. [Frontend Architecture](#frontend-architecture)
7. [REST API Endpoints](#rest-api-endpoints)
8. [External Services](#external-services)
9. [Reconnection & Resilience](#reconnection--resilience)

---

## Project Layout

```
polyapi/
├── app.py                          # FastAPI application — wires engines, defines routes
├── requirements.txt                # Python dependencies
├── GOAL.md                         # Project goals and plan
├── RESEARCH.md                     # Fee model, risk limits, data sources research
├── ARCHITECTURE.md                 # This file — how the live dashboard works
│
├── engines/                        # Server-side data engines
│   ├── __init__.py
│   ├── price_engine.py             # Coinbase WebSocket client for spot prices
│   ├── market_engine.py            # Market discovery + Polymarket CLOB WebSocket
│   └── feed.py                     # WebSocket broadcaster to browser clients
│
├── static/                         # Frontend assets served by FastAPI
│   ├── index.html                  # Main dashboard (React 18, single-file SPA)
│   ├── paper.html                  # Strategy Lab placeholder
│   └── graph-dev.html              # Development graph tool
│
├── tools/                          # Standalone CLI utilities
│   └── polymarket_ws.py            # Polymarket WebSocket explorer (events, CLOB, sports)
│
├── tests/                          # Test/exploration scripts
│   ├── test_kalshi*.py             # Kalshi API exploration (5 files)
│   ├── test_kraken_spot.py         # Kraken WS stress test
│   ├── test_spot_sources.py        # Spot price source benchmark
│   ├── test_rtds.py                # Quick spot price test
│   ├── test_debug_house.py         # Debug utility
│   └── test_book_parse.js          # Order book parsing unit test
│
└── docs/                           # Reference documentation
    ├── BACKTEST_PAPER_ARCHITECTURE.md  # Backtest/paper trading design
    └── QUANT_ENGINE_RESEARCH.md        # Data structures, microstructure research
```

### What each directory is for

| Directory   | Purpose |
|-------------|---------|
| `engines/`  | Long-running async tasks that maintain persistent WebSocket connections to external data sources. They run server-side so the browser never talks directly to Coinbase or Polymarket. |
| `static/`   | Files served directly to the browser. The main `index.html` is a self-contained React app using CDN-loaded React 18, Tailwind CSS, and JetBrains Mono font. |
| `tools/`    | Standalone scripts for debugging and exploration. Not imported by the main app. |
| `tests/`    | All test files, moved here from the project root to reduce clutter. |

---

## How to Run

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
uvicorn app:app --reload

# Open in browser
# http://localhost:8000
```

The `--reload` flag watches for file changes and restarts automatically during development.

### Dependencies

| Package      | Role |
|--------------|------|
| `fastapi`    | Web framework — HTTP routes + WebSocket endpoint |
| `uvicorn`    | ASGI server that runs FastAPI |
| `httpx`      | Async HTTP client for REST API calls to Gamma and CLOB |
| `websockets` | WebSocket client library for connecting to Coinbase and Polymarket |
| `jinja2`     | Template engine (FastAPI dependency for static files) |
| `rapidfuzz`  | Fuzzy string matching (used in tooling) |

---

## Backend Architecture

The backend runs three singleton objects that are created at module import and started/stopped with the FastAPI application lifespan:

```
┌─────────────────────────────────────────────────────────────┐
│                         app.py                              │
│                                                             │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐  │
│  │ PriceEngine  │  │ MarketEngine  │  │      Feed        │  │
│  │              │  │               │  │                  │  │
│  │ Coinbase WS ─┼──┼─── callbacks ─┼──┤► broadcast()     │  │
│  │ BTC/ETH/SOL  │  │               │  │  to all browser  │  │
│  │              │  │ Discovery     │  │  WebSockets      │  │
│  │ on_price() ──┼──┼─── loop ─────┼──┤                  │  │
│  │              │  │               │  │► send_snapshot()  │  │
│  │              │  │ CLOB WS ─────┼──┤  on new connect  │  │
│  │              │  │ on_market_ ───┼──┤                  │  │
│  │              │  │   event()     │  │                  │  │
│  └──────────────┘  └───────────────┘  └──────────────────┘  │
│                                             ▲               │
│                         /ws/feed ───────────┘               │
│                                                             │
│  GET /                  → serves index.html                 │
│  GET /api/markets-5m    → reads MarketEngine.markets        │
│  GET /api/spot-prices   → reads PriceEngine.prices          │
│  GET /api/clob/:id      → proxies to CLOB REST API          │
└─────────────────────────────────────────────────────────────┘
```

### Application Entry Point (`app.py`)

**File:** `app.py`

This is the FastAPI application. It does three things:

1. **Creates engine instances** — `PriceEngine`, `MarketEngine`, and `Feed` are instantiated as module-level singletons.

2. **Wires callbacks** — Each engine accepts callback functions that fire when new data arrives. These callbacks are defined in `app.py` and call `feed.broadcast()` to push data to all connected browsers:

   | Engine callback         | What triggers it                  | What it broadcasts       |
   |-------------------------|-----------------------------------|--------------------------|
   | `on_price(sym, val, ts)` | Every Coinbase spot price tick    | `{"type": "price", ...}` |
   | `on_market_event(data)`  | Every CLOB WebSocket message      | `{"type": "clob", ...}`  |
   | `on_markets_update(mkts)` | Market list refreshed (every 60s) | `{"type": "markets_update", ...}` |

3. **Manages lifespan** — The `lifespan` async context manager starts both engines when the server boots and stops them on shutdown:

   ```python
   @asynccontextmanager
   async def lifespan(app):
       await price_engine.start()     # launches Coinbase WS task
       await market_engine.start()    # launches discovery + CLOB WS tasks
       yield
       await price_engine.stop()
       await market_engine.stop()
   ```

**Routes defined:**

| Route                    | Method    | Description |
|--------------------------|-----------|-------------|
| `/`                      | GET       | Serves `static/index.html` with no-cache headers |
| `/ws/feed`               | WebSocket | Single unified feed to the browser |
| `/api/markets-5m`        | GET       | Returns `market_engine.markets` (in-memory, no external call) |
| `/api/spot-prices`       | GET       | Returns `price_engine.prices` (in-memory, no external call) |
| `/api/clob/{condition_id}` | GET     | Proxies a single CLOB market lookup to Polymarket |

---

### PriceEngine (`engines/price_engine.py`)

**Purpose:** Maintains a persistent WebSocket connection to Coinbase and streams real-time spot prices for BTC, ETH, and SOL.

**How it works:**

1. On `start()`, it spawns an `asyncio.Task` that runs the `_run()` loop.
2. `_run()` connects to `wss://ws-feed.exchange.coinbase.com` and subscribes to the `ticker` channel for three products: `BTC-USD`, `ETH-USD`, `SOL-USD`.
3. For each incoming `ticker` message, it:
   - Extracts the `price` and `product_id`
   - Maps the Coinbase product ID to an internal symbol (e.g., `BTC-USD` becomes `btcusdt`)
   - Stores the latest price in `self.prices` (a dict: `{ "btcusdt": {"value": 87432.10, "ts": 1710000000000}, ... }`)
   - Calls the `on_price` callback so the data can be broadcast to browsers

**Key attributes:**

| Attribute     | Type                       | Description |
|---------------|----------------------------|-------------|
| `prices`      | `dict[str, dict]`          | Latest price per symbol. Keys: `btcusdt`, `ethusdt`, `solusdt`. Values: `{"value": float, "ts": int}` |
| `_on_price`   | `Callable(sym, value, ts)` | Callback fired on every tick |
| `_running`    | `bool`                     | Controls the run loop |

**Product mapping:**

| Coinbase product | Internal symbol |
|------------------|-----------------|
| `BTC-USD`        | `btcusdt`       |
| `ETH-USD`        | `ethusdt`       |
| `SOL-USD`        | `solusdt`       |

---

### MarketEngine (`engines/market_engine.py`)

**Purpose:** Discovers active 5-minute crypto prediction markets on Polymarket and streams live orderbook/trade data from the CLOB WebSocket.

**It runs two concurrent tasks:**

#### 1. Discovery Loop (`_discovery_loop`)

Runs every 60 seconds. Each cycle:

1. **Generates slug candidates** — For each asset (`btc`, `eth`, `sol`), it builds time-windowed slugs like `btc-updown-5m-1710000000` by computing the current 5-minute window and generating candidates for ±4 windows.
2. **Fetches events from Gamma API** — Sends parallel HTTP requests to `https://gamma-api.polymarket.com/events/slug/{slug}` for all candidates.
3. **Resolves CLOB token IDs** — For each valid event, it fetches the market from `https://clob.polymarket.com/markets/{conditionId}` and extracts the `token_id` for the "Yes" or "Up" outcome.
4. **Filters expired markets** — Skips any market whose end timestamp is in the past.
5. **Updates state** — Stores the resulting market list in `self.markets` and compares token IDs. If the set of token IDs changed, it signals the CLOB loop to reconnect with the new subscription.
6. **Fires callback** — Calls `on_markets_update(markets)` so the feed can broadcast the new list and update its snapshot.

**Market object shape** (each entry in `self.markets`):

```json
{
  "id": "event-uuid",
  "question": "Will BTC go up in the next 5 minutes?",
  "conditionId": "0xabc...",
  "yesTokenId": "12345...",
  "asset": "BTC",
  "slug": "btc-updown-5m-1710000000",
  "endDate": "2026-03-09T23:05:00+00:00",
  "volume": 15234.5,
  "liquidity": 8912.3,
  "yes_pct": 52.1
}
```

#### 2. CLOB WebSocket Loop (`_clob_loop`)

1. **Waits for token IDs** — Blocks on `_ids_changed` event until the discovery loop provides at least one token ID.
2. **Connects to CLOB WebSocket** — `wss://ws-subscriptions-clob.polymarket.com/ws/market`
3. **Subscribes** — Sends `{"assets_ids": [...], "type": "market", "custom_feature_enabled": true}`.
4. **Streams events** — Forwards every parsed JSON message to `on_market_event(data)`.
5. **Keepalive** — Sends `"PING"` every 9 seconds; ignores `"PONG"` responses.
6. **Resubscribes on change** — If the discovery loop updates the token IDs (sets `_ids_changed`), the CLOB loop breaks out of its inner loop, closing the current connection, and reconnects with the new subscription.

**CLOB event types received:**

| `event_type`       | Contains |
|--------------------|----------|
| `book`             | Full orderbook snapshot: `bids`, `asks` arrays |
| `best_bid_ask`     | Top-of-book update: `best_bid`, `best_ask`, `spread` |
| `last_trade_price` | Trade execution: `price`, `side`, `size` |
| `price_change`     | Price movement with `price_changes` array |

---

### Feed (`engines/feed.py`)

**Purpose:** Manages all browser WebSocket connections and broadcasts messages from the engines to every connected client.

**How it works:**

1. **Connection management** — When a browser connects to `/ws/feed`, `app.py` calls `feed.connect(ws)` which accepts the WebSocket and adds it to the `_clients` set. On disconnect, `feed.disconnect(ws)` removes it.

2. **Snapshot on connect** — Immediately after accepting a new connection, `send_snapshot()` sends the current state (latest prices + market list) so the browser doesn't have to wait for the next tick:

   ```json
   {
     "type": "snapshot",
     "prices": {"btcusdt": {"value": 87432.10, "ts": 1710000000000}, ...},
     "markets": [{...}, {...}],
     "ts": 1710000000000
   }
   ```

3. **Broadcasting** — `broadcast(message)` serializes the message to JSON once and sends it to every client in `_clients`. Dead connections (those that raise an exception on send) are automatically removed.

4. **Snapshot updates** — The engines keep the snapshot data fresh by calling `update_snapshot_prices()` and `update_snapshot_markets()` whenever new data arrives. This ensures newly connecting clients always get the latest state.

**Key attributes:**

| Attribute        | Type               | Description |
|------------------|--------------------|-------------|
| `_clients`       | `set[WebSocket]`   | All active browser connections |
| `_snapshot_data`  | `dict`             | Latest prices and markets, sent to new connections |
| `client_count`   | `int` (property)   | Number of connected browsers |

---

## Data Flow

Here is the complete path data travels from external source to the user's browser:

```
External Sources                    Backend (Python)                    Browser (JS)
─────────────────                   ─────────────────                   ─────────────

Coinbase WS ──────►  PriceEngine
  ticker msgs          stores in self.prices
                       calls on_price() ──────►  Feed.broadcast()
                                                   {"type": "price"}  ──────►  useBackendFeed
                                                                                 onPrice()
                                                                                 updateSpotPrice()

Gamma REST API ───►  MarketEngine
  /events/slug/        _discovery_loop (every 60s)
CLOB REST API ────►    resolves token IDs
  /markets/            stores in self.markets
                       calls on_markets_update() ─►  Feed.broadcast()
                                                      {"type":              ──────►  useBackendFeed
                                                       "markets_update"}              onMarketsUpdate()
                                                                                      setMarkets()

CLOB WS ──────────►  MarketEngine
  orderbook/trades     _clob_loop
                       calls on_market_event() ───►  Feed.broadcast()
                                                      {"type": "clob"}    ──────►  useBackendFeed
                                                                                    onClob()
                                                                                    handleCLOB()

                                    On new browser connection:
                                    Feed.send_snapshot() ─────────────────►  onSnapshot()
                                      {"type": "snapshot",                    hydrates initial
                                       prices, markets}                       state immediately
```

---

## WebSocket Protocol

All messages flow from backend to browser over a single WebSocket at `/ws/feed`. Every message is a JSON object with a `type` field.

### Message Types

#### `snapshot`
Sent once when a browser first connects. Contains the full current state.

```json
{
  "type": "snapshot",
  "prices": {
    "btcusdt": { "value": 87432.10, "ts": 1710000000000 },
    "ethusdt": { "value": 3245.50, "ts": 1710000000000 },
    "solusdt": { "value": 142.30, "ts": 1710000000000 }
  },
  "markets": [
    {
      "id": "...",
      "question": "Will BTC go up...?",
      "conditionId": "0x...",
      "yesTokenId": "123...",
      "asset": "BTC",
      "slug": "btc-updown-5m-...",
      "endDate": "2026-03-09T23:05:00+00:00",
      "volume": 15234.5,
      "liquidity": 8912.3,
      "yes_pct": 52.1
    }
  ],
  "ts": 1710000000000
}
```

#### `price`
Sent on every spot price tick from Coinbase (multiple times per second).

```json
{
  "type": "price",
  "symbol": "btcusdt",
  "value": 87433.25,
  "ts": 1710000000123
}
```

#### `clob`
Passthrough of raw CLOB WebSocket events (orderbook updates, trades, best bid/ask changes).

```json
{
  "type": "clob",
  "data": {
    "event_type": "best_bid_ask",
    "asset_id": "123...",
    "best_bid": "0.52",
    "best_ask": "0.54",
    "spread": "0.02"
  }
}
```

#### `markets_update`
Sent every 60 seconds when the discovery loop refreshes the market list.

```json
{
  "type": "markets_update",
  "markets": [ /* same shape as snapshot.markets */ ]
}
```

---

## Frontend Architecture

The frontend is a single-file React 18 SPA in `static/index.html`. It uses CDN-loaded React (no build step) and `React.createElement` calls (no JSX).

### Key Hook: `useBackendFeed`

This is the only data connection the browser makes. It replaces three separate hooks from the previous architecture (`useSpotWebSocket`, `useCLOB`, `useSpotPricesRest`).

```
Previous (3 connections):              Current (1 connection):
  Browser ──► Coinbase WS                Browser ──► /ws/feed (backend)
  Browser ──► Polymarket CLOB WS                        │
  Browser ──► /api/spot-prices REST              receives all data as
                                                 unified JSON messages
```

**How `useBackendFeed` works:**

1. Builds the WebSocket URL from `location.host` (auto-detects `ws:` vs `wss:` based on page protocol).
2. Connects to `/ws/feed` on the same host.
3. Routes incoming messages by `type` to handler callbacks:
   - `onSnapshot` — hydrates prices and markets on first connect
   - `onPrice` — updates spot price state
   - `onClob` — processes orderbook/trade updates
   - `onMarketsUpdate` — replaces market list
4. Auto-reconnects on close with exponential backoff (1s, 2s, 4s, ... up to 30s).
5. Returns a `status` string: `'disconnected'` | `'connecting'` | `'connected'`.

### Visual Components (unchanged)

| Component         | What it renders |
|-------------------|-----------------|
| `Header`          | Title bar with single "Feed" connection status dot and message counter |
| `SpotPriceRow`    | Current price, % change, 5-minute window sparkline chart |
| `Sparkline`       | SVG sparkline with gradient fill (green = up, red = down) |
| `MarketCard`      | Prediction market card with probability gauge, bid/ask spread, orderbook depth, trade history |
| `ProbabilityGauge`| Half-circle SVG gauge showing Up probability percentage |
| `CountdownTimer`  | MM:SS countdown to market close |
| `ActivityLog`     | Scrolling log of recent events |

---

## REST API Endpoints

These endpoints read directly from engine in-memory state. They make no external API calls (except `/api/clob/:id` which proxies to Polymarket).

| Endpoint                  | Method | Response | Notes |
|---------------------------|--------|----------|-------|
| `/`                       | GET    | HTML     | Dashboard page, served with no-cache headers |
| `/api/markets-5m`         | GET    | JSON array | Current discovered markets from `MarketEngine.markets` |
| `/api/spot-prices`        | GET    | JSON object | Latest prices from `PriceEngine.prices` |
| `/api/clob/{condition_id}`| GET    | JSON object | Proxied CLOB market data for a specific condition |

The first two are useful for debugging — you can `curl localhost:8000/api/spot-prices` to see what the engine has without needing a browser.

---

## External Services

| Service | URL | Protocol | Used By | Purpose |
|---------|-----|----------|---------|---------|
| Coinbase Exchange | `wss://ws-feed.exchange.coinbase.com` | WebSocket | PriceEngine | Real-time BTC/ETH/SOL spot prices |
| Polymarket Gamma API | `https://gamma-api.polymarket.com` | REST | MarketEngine | Discover active 5-min prediction markets |
| Polymarket CLOB API | `https://clob.polymarket.com` | REST | MarketEngine, app.py | Resolve token IDs, proxy market data |
| Polymarket CLOB WS | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | WebSocket | MarketEngine | Stream orderbook and trade events |

---

## Reconnection & Resilience

Both engines implement automatic reconnection with exponential backoff:

| Behavior | PriceEngine | MarketEngine (CLOB) | Frontend (useBackendFeed) |
|----------|-------------|---------------------|---------------------------|
| Initial backoff | 1 second | 1 second | 1 second |
| Backoff multiplier | 2x | 2x | 2x |
| Max backoff | 30 seconds | 30 seconds | 30 seconds |
| Reset on success | Yes — back to 1s | Yes — back to 1s | Yes — back to 1s |
| Keepalive | Not needed (Coinbase sends ticks) | Sends `"PING"` every 9s | None needed (server holds connection) |

**MarketEngine resubscription:** When the discovery loop finds new markets (different token IDs), it sets an `asyncio.Event`. The CLOB loop checks this event on every iteration — if set, it breaks out, closes the WebSocket, and reconnects with the updated subscription list. This means the CLOB stream always tracks the latest active markets.

**Feed dead connection cleanup:** When `broadcast()` fails to send to a client (connection dropped), that client is removed from the `_clients` set automatically. No explicit heartbeat is needed.

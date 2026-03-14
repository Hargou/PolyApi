# PolyApi

**Event-driven quant engine for Polymarket's 5-minute crypto binary options.** Backtest strategies against historical data, paper trade against live WebSocket feeds, and analyze performance with institutional-grade metrics -- all from a single unified execution pipeline.

Targets BTC, ETH, and SOL Up/Down prediction markets on Polymarket's CLOB (Central Limit Order Book) with 5-minute expiry windows.

---

## Architecture

```
                         +-----------------------+
                         |   Strategy Layer      |
                         |  BaseStrategy.evaluate |
                         |  -> Signal(action,     |
                         |     size, p_hat, ev)   |
                         +-----------+-----------+
                                     |
                    +----------------v-----------------+
                    |        StrategyRunner             |
                    |  Event loop: spot, clob,          |
                    |  market_info, resolution          |
                    |  Builds MarketState, routes       |
                    |  signals through execution        |
                    +---+---------------------+--------+
                        |                     |
               +--------v--------+  +---------v---------+
               |   Backtest       |  |   Paper Trading    |
               |  ReplaySource    |  |   LiveSource       |
               |  Historical API  |  |   3x WebSocket     |
               |  Synthetic books |  |   L2 REST polling   |
               +--------+--------+  +---------+----------+
                        |                      |
                        +----------+-----------+
                                   |
                    +--------------v--------------+
                    |     Execution Pipeline       |
                    |  RiskEngine -> OrderManager   |
                    |  -> FillSimulator -> Portfolio |
                    +------------------------------+
```

**Key design choice:** The `StrategyRunner` consumes an `Event` stream agnostically -- the same runner, same strategy code, same risk checks, and same fill simulation run in both backtest and live modes. The only difference is the event source.

---

## Execution Pipeline

### Non-Linear Fee Model

Polymarket's crypto taker fee is **not** a flat percentage. It follows a non-linear curve that peaks at 50% probability and approaches zero at the extremes:

```
fee = size * price * 0.25 * (price * (1 - price))^2
```

| Contract Price | Effective Rate | Fee on 100 contracts |
|:--------------:|:--------------:|:--------------------:|
| 5c / 95c       | 0.0006%        | $0.0003              |
| 20c / 80c      | 0.064%         | $0.0128              |
| 35c / 65c      | 0.324%         | $0.1134              |
| 50c            | 0.391%         | $0.1953              |

This matters: a strategy that looks profitable under a flat-fee assumption can be underwater once the real curve is applied, especially for contracts trading near 50%.

### L2 Order Book Walk

Fills are simulated by walking the actual L2 order book level-by-level, not by assuming a midpoint fill:

```python
def walk_book(levels, target_size):
    """Walk price levels best-to-worst, compute VWAP fill."""
    remaining = target_size
    for price, size in levels:
        take = min(remaining, size)
        notional += take * price
        remaining -= take
    return notional / filled  # volume-weighted average price
```

This produces slippage estimates within 10-30 bps of real fills -- tight enough to validate strategy edge before risking capital.

### Risk Engine

All risk parameters are encapsulated in a single `RiskConfig` dataclass -- every field is sweepable in backtests:

```python
@dataclass
class RiskConfig:
    max_position_per_market: int = 500
    max_total_exposure: float = 5000.0
    max_concurrent_positions: int = 6
    max_loss_per_window: float = 200.0
    max_drawdown_pct: float = 10.0      # circuit breaker
    max_spread_bps: float = 500.0
    min_remaining_sec: int = 30
    max_elapsed_sec: int = 240
    cooldown_after_loss_sec: int = 0
```

The risk engine sits between strategy signals and order execution. Every signal passes through spread checks, exposure limits, drawdown circuit breakers, timing guards, and position concentration limits before a fill is simulated.

---

## Strategy Interface

```python
class BaseStrategy(ABC):
    exit_policy: ExitPolicy = ExitPolicy.HOLD_TO_EXPIRY

    @abstractmethod
    def evaluate(self, state: MarketState) -> Signal:
        """Produce a trading signal given current market state."""

    def should_exit(self, state: MarketState, position: Position) -> bool:
        """Override for dynamic mid-window exits."""
        return False
```

`MarketState` contains 20+ fields: orderbook depth, spread, midpoint, spot price, spot return since window open, elapsed/remaining time, and market metadata. Strategies see a complete snapshot with zero lookahead bias.

### Included Strategies

| Strategy | Description |
|----------|-------------|
| `SpotMomentumStrategy` | Bayesian logistic estimate of P(up) from spot return, fractional Kelly sizing, configurable edge/spread/timing thresholds |
| `AlwaysYesStrategy` | Benchmark: buys YES on every market |
| `AlwaysNoStrategy` | Benchmark: buys NO on every market |
| `RandomStrategy` | Benchmark: random side, seeded RNG for reproducibility |

The spot momentum strategy applies a **logistic transform** of the spot return as a Bayesian P(up) estimate, then sizes using **fractional Kelly criterion**:

```
P(up) = logistic(spot_return / scale)
edge  = P(up) - market_midpoint
kelly = edge / (P(up) * (1 - P(up))) * fraction
size  = min(bankroll * kelly / price, max_size)
```

Only trades when edge exceeds `min_edge_bps` (default 300), spread is under `max_spread_bps` (default 400), and elapsed time is between 30s and 180s.

---

## Live Data Pipeline

Three concurrent WebSocket connections feed the paper trading engine:

| Source | Protocol | Data |
|--------|----------|------|
| Coinbase Exchange | `wss://ws-feed.exchange.coinbase.com` | BTC/ETH/SOL spot prices (sub-second) |
| Polymarket CLOB | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | L2 book updates, best bid/ask, trades |
| Polymarket Gamma | REST polling (60s) | Market discovery, condition IDs, token resolution |

Additionally, the paper trader polls full L2 order books via REST every N seconds (configurable) to maintain accurate fill simulation depth.

`LiveSource` chains into the existing engine callbacks, translates raw WebSocket events into typed `Event` objects, and pushes them into an `asyncio.Queue`. The paper trading runner consumes events from the same queue interface that the backtest replay source provides.

---

## Usage

### Backtest

```bash
# Single strategy against recent resolved markets
python -m cli.backtest --strategy spot_momentum --markets 10

# Compare all strategies on the same data
python -m cli.backtest --all --markets 20

# Custom bankroll
python -m cli.backtest --strategy spot_momentum --bankroll 50000
```

### Paper Trade

```bash
# Default: spot_momentum, unlimited duration
python -m cli.paper

# Run for 1 hour with faster book polling
python -m cli.paper --strategy spot_momentum --duration 3600 --book-poll 5

# Run a benchmark strategy live
python -m cli.paper --strategy always_yes --duration 1800
```

Status line prints every 30 seconds. Full trade log and settlement summary on exit (Ctrl+C).

### Live Dashboard

```bash
uvicorn app:app --reload
# Open http://localhost:8000
```

Real-time dashboard showing spot prices, active prediction markets, probability gauges, orderbook depth, and trade activity across BTC/ETH/SOL.

---

## Project Structure

```
polyapi/
├── strategies/
│   ├── base.py                 # BaseStrategy, MarketState, Signal, Position, ExitPolicy
│   ├── spot_momentum.py        # Bayesian logistic + Kelly sizing strategy
│   └── benchmarks.py           # AlwaysYes, AlwaysNo, Random baselines
│
├── execution/
│   ├── fees.py                 # Non-linear Polymarket fee curve
│   ├── fill_simulator.py       # L2 book walk, slippage estimation
│   ├── risk_engine.py          # RiskConfig (11 params), pre-trade checks
│   ├── order_manager.py        # Signal -> risk -> fill -> portfolio pipeline
│   ├── portfolio.py            # Positions, trades, settlement, PnL tracking
│   └── runner.py               # Event-driven StrategyRunner (backtest + live)
│
├── data/
│   ├── models.py               # Event, SpotTick, ClobSnapshot, MarketInfo, MarketResolution
│   ├── fetcher.py              # Polymarket REST API (price history, books, discovery)
│   ├── replay_source.py        # Historical data -> Event stream for backtesting
│   └── live_source.py          # WebSocket -> Event stream for paper trading
│
├── analysis/
│   └── metrics.py              # PnL, win rate, drawdown, profit factor, Sharpe
│
├── cli/
│   ├── backtest.py             # CLI: python -m cli.backtest
│   └── paper.py                # CLI: python -m cli.paper
│
├── engines/                    # Live dashboard data engines
│   ├── price_engine.py         # Coinbase WebSocket (spot prices)
│   ├── market_engine.py        # Market discovery + CLOB WebSocket
│   └── feed.py                 # WebSocket broadcaster to browser clients
│
├── static/                     # React 18 SPA dashboard (no build step)
│   └── index.html
│
└── app.py                      # FastAPI server (dashboard + API)
```

---

## Metrics Output

```
============================================================
  BACKTEST RESULTS: spot_momentum
============================================================

  Net PnL:          $   142.37
  Total Fees:       $    8.2134
  Bankroll:         $ 10142.37  (started $10000.00)

  Trades:                   23
  Wins:                     14
  Losses:                    9
  Win Rate:              60.9%

  Avg PnL/trade:    $     6.19
  Avg Win:          $    18.42
  Avg Loss:         $   -12.88
  Max Win:          $    47.31
  Max Loss:         $   -28.14

  Max Drawdown:     $    52.80  (0.53%)
  Profit Factor:         1.59
============================================================
```

---

## Setup

```bash
pip install -r requirements.txt
```

Dependencies: `fastapi`, `uvicorn`, `httpx`, `websockets`, `jinja2`, `rapidfuzz`

Python 3.11+

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| Hold-to-expiry default | 5-min windows are too short for profitable mid-window exits after fees and slippage. Dynamic exit via `should_exit()` hook available for strategies that want it. |
| Event-driven, no lookahead | Sequential event processing prevents future data leaking into signals. Same code path for backtest and live. |
| Non-linear fee model | Polymarket's actual curve, not a flat approximation. Critical for edge detection near 50%. |
| L2 book walk for fills | Walking real depth is more accurate than midpoint or VWAP assumptions. 10-30 bps error vs real exchange. |
| Fractional Kelly sizing | Full Kelly is too aggressive for binary outcomes with estimation error. Default 0.25x Kelly. |
| No VPS required | Historical data from Polymarket's `/prices-history` API (1-min candles). Free, no infrastructure. |
| Dataclass-only config | `RiskConfig` and `SpotMomentumConfig` are plain dataclasses. Easy to serialize, sweep, and version. |

---

## License

MIT

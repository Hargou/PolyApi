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

### Rust Replay Engine

The backtest engine is written in Rust (PyO3 + maturin) for performance. It reads Parquet data, builds MarketState in Rust, and calls Python strategy callbacks via GIL. Processes 9.2M rows in ~170s.

```bash
cd rust_engine
maturin build --release
pip install target/wheels/poly_engine-*.whl
```

```python
import poly_engine
results = poly_engine.run_replay("data.parquet", callbacks, 10_000.0)
```

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
    max_spread_bps: float = 1000.0
    min_remaining_sec: int = 5
    max_elapsed_sec: int = 295
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

**Core insight:** Polymarket's non-linear fee formula means fees are near-zero at price extremes (<0.22 or >0.78) but ~1.56% at midpoint. The only consistently profitable approach is trading at extremes where the fee structure gives a 300+ bps structural edge.

#### Production Strategies

| Strategy | Logic | Backtest PnL |
|----------|-------|:------------:|
| `ComboAlphaStrategy` | Fee-extremes gatekeeper + multi-signal (spot momentum, OBI, microprice). Dual mode: CONFIRM (signals agree) or FADE (early-window extreme reversion). Best performer. | **+$460** |
| `EarlyFadeStrategy` | Fades early-window overreactions at price extremes (<0.22 or >0.78). When price hits extreme in first 90s, buy cheap contracts against the extreme. Fees near-zero at extremes (structural bonus). | **+$291** |
| `EarlyFadeV2Strategy` | V1 + Bayesian p_hat (replaces logistic), relaxed timing (5-295s), cross-asset momentum, confidence-weighted Kelly. | **+$291** |

#### Research Strategies (active)

| Strategy | Logic | Status |
|----------|-------|--------|
| `BinaryReversalStrategy` | Vol regime filter + FADE: skips CALM/STORM, fades NORMAL-vol extremes | Near breakeven (-$16, 70 trades) |
| `MicrostructureFadeStrategy` | OBI confirms early extreme reversals (first 60s) | Needs real L2 book data from VPS |

#### Archived Strategies (strategies/archive/)

Removed after backtest analysis showed fundamental issues:

| Strategy | Why Archived |
|----------|-------------|
| `time_decay` | -$1,070 on 57 trades, 40.4% win, PF 0.55. Unbounded memory growth caused 9x perf regression |
| `quant_models` | -$712 on 119 trades, 47.1% win, PF 0.79. Composite of weak signals, trades near midpoint |
| `orderbook_imbalance` | 0 trades — synthetic book data has no meaningful OBI signal |
| `volatility_regime` | 0 trades — Brownian model at extremes doesn't produce sufficient edge |
| `spot_momentum` + `v2` | Weak signal, trades at max-fee midpoint, 46.5% win |
| `snipe` | Prices repriced by T-30s, wide spreads, 10.5% win |
| `consensus` | Voting on weak signals = false consensus, 8.3% win |
| `liquidity_vacuum` | Theory backward (book thinning = risk mgmt, not signal) |
| `quant_models_v2` | Marginal reweight of broken approach |

#### Utility Modules

| Module | Purpose |
|--------|---------|
| `bayesian.py` | Beta-Bernoulli sequential P(up) estimator, confidence-weighted Kelly |
| `vol_utils.py` | Yang-Zhang OHLC vol estimator, TickVolTracker, implied vol inversion |
| `benchmarks.py` | AlwaysYes, AlwaysNo, Random baselines |

#### Key Research Findings

- **Early-window fading is the only profitable approach** — price at extreme in first 90s is often an overreaction
- **Fee structure provides structural bonus** — fees near-zero at extremes (0.06% at 0.15 vs 1.56% at 0.50)
- **Relaxed risk limits matter** — loosening min_remaining_sec from 30→5 and max_elapsed_sec from 240→295 gave 5x PnL improvement
- **Kelly cap dominates sizing** — all strategies hit max_size, making Kelly fraction less relevant than max_size

---

## Live Data Pipeline

Five concurrent data streams feed the engine:

| Source | Protocol | Data |
|--------|----------|------|
| Coinbase Exchange | `wss://ws-feed.exchange.coinbase.com` | BTC/ETH/SOL spot prices (sub-second) |
| Chainlink Price Feeds | Polygon RPC (on-chain, 5s poll) | Oracle prices used for Polymarket resolution |
| Polymarket CLOB | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | L2 book updates, best bid/ask, trades |
| Polymarket Gamma | REST polling (60s) | Market discovery, condition IDs, token resolution |
| Resolution detection | REST polling (30s) | Monitors expired markets for YES/NO outcome |

**Polymarket resolves via Chainlink Data Streams**, not Coinbase. The ~0.3 bps difference between exchanges can flip outcomes on tight (<5 bps) moves. Chainlink on-chain feeds on Polygon approximate the oracle price for strategy calibration.

---

## Usage

### VPS Data Collection

Record 24/7 tick data on a VPS for backtesting:

```bash
# Deploy collector to a fresh Ubuntu VPS
bash collector/setup_vps.sh

# Or manually:
python -m collector.recorder --output data_store/
```

The recorder writes JSONL files (one per day) with spot, chainlink, clob, market_info, and resolution events.

### Sync & Replay

```powershell
# Download data from VPS
.\collector\sync_data.ps1

# Replay through all 11 strategies
python -m collector.replay data_store/ --all

# Single strategy
python -m collector.replay data_store/ --strategy quant_models
```

### Backtest (API data)

```bash
# Single strategy against recent resolved markets
python -m cli.backtest --strategy quant_models --markets 10

# Compare all strategies on the same data
python -m cli.backtest --all --markets 20
```

### Paper Trade

```bash
# Default: quant_models, unlimited duration
python -m cli.paper

# Run for 1 hour with faster book polling
python -m cli.paper --strategy quant_models --duration 3600 --book-poll 5
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
│   ├── early_fade.py           # Production: fade early-window overreactions at extremes
│   ├── early_fade_v2.py        # V2: Bayesian signal + relaxed timing + cross-asset
│   ├── binary_reversal.py      # Vol-weighted FADE: skips CALM/STORM regimes
│   ├── fee_extremes.py         # Legacy name (same logic as early_fade)
│   ├── combo_alpha.py          # Production: fee-extremes + multi-signal (FADE + CONFIRM)
│   ├── microstructure_fade.py  # Research: OBI-confirmed early extreme reversals
│   ├── bayesian.py             # Beta-Bernoulli P(up) estimator
│   ├── vol_utils.py            # Yang-Zhang vol, TickVolTracker
│   ├── benchmarks.py           # AlwaysYes, AlwaysNo, Random baselines
│   └── archive/               # Archived strategies (see README for reasons)
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
├── collector/
│   ├── recorder.py             # 5-stream data recorder (Coinbase, Chainlink, CLOB, Gamma, resolution)
│   ├── replay.py               # Replay JSONL data through all 11 strategies
│   ├── setup_vps.sh            # One-command VPS deployment
│   ├── sync_data.ps1           # Download data from VPS (PowerShell)
│   ├── sync_data.sh            # Download data from VPS (Bash)
│   └── DEPLOY.md               # VPS deployment guide
│
├── analysis/
│   └── metrics.py              # PnL, win rate, drawdown, profit factor, Sharpe
│
├── cli/
│   ├── backtest.py             # CLI: python -m cli.backtest
│   ├── paper.py                # CLI: python -m cli.paper
│   └── sweep.py                # CLI: parameter sweep across risk configs
│
├── engines/                    # Live dashboard data engines
│   ├── price_engine.py         # Coinbase WebSocket (spot prices)
│   ├── market_engine.py        # Market discovery + CLOB WebSocket
│   ├── paper_session.py        # Paper trading session manager
│   └── feed.py                 # WebSocket broadcaster to browser clients
│
├── static/                     # React 18 SPA dashboard (no build step)
│   └── index.html
│
├── rust_engine/                # Rust replay engine (PyO3 + maturin)
│   ├── src/
│   │   ├── lib.rs              # PyO3 module entry
│   │   ├── replay.rs           # Main event loop, Parquet reader
│   │   ├── state.rs            # MarketState construction
│   │   ├── fill.rs             # L2 order book walking
│   │   ├── fees.rs             # Non-linear fee calculation
│   │   └── types.rs            # Data types, RiskConfig
│   ├── Cargo.toml
│   └── pyproject.toml
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

Dependencies: `fastapi`, `uvicorn`, `httpx`, `websockets`, `jinja2`, `rapidfuzz`, `web3` (optional, for Chainlink feeds)

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
| Chainlink oracle alignment | Polymarket resolves via Chainlink, not Coinbase. ~0.3 bps diff can flip tight outcomes. Collector records both. |
| VPS recorder | 24/7 tick-level data collection (spot, chainlink, CLOB, market info, resolution) for offline replay. |
| Dataclass-only config | All strategy and risk configs are plain dataclasses. Easy to serialize, sweep, and version. |

---

## Documentation

| Doc | Contents |
|-----|----------|
| [docs/research/RESEARCH.md](docs/research/RESEARCH.md) | Strategy research log — findings, leaderboard, planned investigations |
| [docs/research/PARAM_LOG.md](docs/research/PARAM_LOG.md) | Every parameter change with before/after results |
| [docs/DATA_PIPELINE.md](docs/DATA_PIPELINE.md) | VPS sync, JSONL→Parquet conversion, Parquet schema, data sizes |
| [docs/BACKTEST_PAPER_ARCHITECTURE.md](docs/BACKTEST_PAPER_ARCHITECTURE.md) | Detailed backtest design |
| [docs/QUANT_ENGINE_RESEARCH.md](docs/QUANT_ENGINE_RESEARCH.md) | Data structures, microstructure |
| [RESEARCH.md](RESEARCH.md) | Fee model, exit policy, risk limits, data sources |
| [GOAL.md](GOAL.md) | Project goals + implementation plan (6 phases) |
| [collector/DEPLOY.md](collector/DEPLOY.md) | VPS deployment guide |

---

## License

MIT

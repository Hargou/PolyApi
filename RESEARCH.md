# PolyApi — Research & Design Decisions

**Updated:** 2026-03-14
**Scope:** Fee model, exit policy, window start price, risk limits, data recording, paper trading engine

---

## 1. Polymarket Fee Model (Verified from Docs)

### How Fees Work

**Takers pay everything. Makers pay zero** (and receive a 20% rebate from taker fees).

Fees apply to crypto markets. The formula is non-linear and depends on the contract price (probability):

```
fee = contracts * price * fee_rate * (price * (1 - price)) ^ exponent
```

| Parameter | Crypto Markets |
|-----------|---------------|
| `fee_rate` | 0.25 |
| `exponent` | 2 |
| Maker rebate | 20% of taker fee |

### Effective Fee Rates by Probability

| Contract Price | Effective Rate | Fee on 100 contracts |
|---------------|---------------|---------------------|
| $0.05 (5%) | ~0.006% | $0.0003 |
| $0.10 (10%) | ~0.20% | $0.02 |
| $0.20 (20%) | ~0.64% | $0.13 |
| $0.30 (30%) | ~1.10% | $0.33 |
| $0.40 (40%) | ~1.44% | $0.58 |
| **$0.50 (50%)** | **~1.56% (max)** | **$0.78** |
| $0.70 (70%) | ~1.10% | $0.77 |
| $0.90 (90%) | ~0.20% | $0.18 |

The fee curve is **symmetric around 50%** and **approaches zero at extremes** (near 1c and 99c).

### What This Means for Strategy Edge

A strategy needs to clear ~1.56% at 50/50 markets (worst case) just to break even on fees. In practice, 5-min crypto markets often trade at 45-55% probability, so expect ~1.2-1.5% fee drag per entry. **A strategy showing +80 bps gross edge can go negative after fees at 50% markets.**

### Fee API Endpoint

```
GET https://clob.polymarket.com/fee-rate?token_id={token_id}
→ {"base_fee": 30}  (basis points)
```

### Implementation

```python
def polymarket_fee(price: float, size: float, fee_rate: float = 0.25, exponent: int = 2) -> float:
    """Exact Polymarket crypto taker fee. Returns fee in dollars."""
    return size * price * fee_rate * (price * (1.0 - price)) ** exponent

def polymarket_maker_rebate(taker_fee: float, rebate_pct: float = 0.20) -> float:
    """Maker rebate = 20% of taker fee."""
    return taker_fee * rebate_pct
```

### Settlement

- No settlement fee. Winners redeem tokens for $1 each, losers get $0.
- Resolution via UMA Optimistic Oracle (~2 hour challenge window).
- No fee on profit — only on the transaction itself.

**Source:** [Polymarket Fees](https://docs.polymarket.com/trading/fees), CLOB fee-rate API

---

## 2. Exit Policy

### Two Modes (Configurable Per Strategy)

| Policy | How It Works | When to Use |
|--------|-------------|-------------|
| **Hold to Expiry** | Buy position, hold until market resolves. PnL = payout - cost - fees. | Default for 5-min markets. No exit slippage. |
| **Dynamic Exit** | Sell before expiry if edge flips or stop-loss hit. Incur spread + fee again. | Longer windows, or if market moves strongly against you. |

### Why Hold-to-Expiry is the Default

For 5-minute crypto markets:
- Spreads are often 2-5 cents (200-500 bps). Exiting early means paying spread + taker fee again.
- The window is too short for meaningful mid-window edge changes.
- A round-trip (enter + exit) costs 2x the taker fee + 2x the spread crossing.

### Dynamic Exit — When It Makes Sense

Only worth it when the expected loss from holding exceeds the cost of exiting:

```
exit_if: expected_loss_from_holding > spread_cost + exit_fee
```

Example: You bought YES at $0.52. Market moves to YES = $0.35 with 3 minutes left. Expected payout if held = $0.35. Loss from holding = $0.17. Exit cost = ~$0.03 (spread) + ~$0.01 (fee) = $0.04. Net savings from exit = $0.17 - $0.04 = $0.13. **Exit is correct here.**

### Implementation

```python
class ExitPolicy(Enum):
    HOLD_TO_EXPIRY = "hold"
    DYNAMIC = "dynamic"

class BaseStrategy:
    exit_policy: ExitPolicy = ExitPolicy.HOLD_TO_EXPIRY

    def should_exit(self, state: MarketState, position: Position) -> bool:
        """Override for DYNAMIC exit. Called every tick if exit_policy == DYNAMIC."""
        return False
```

Both modes are testable — run backtests with each policy and compare net PnL.

---

## 3. Window Start Price

### The Problem

Strategies need to know the spot price at the start of the 5-minute window to compute the return so far. For example: "BTC was $84,000 at window start, now it's $84,200, that's a +24 bps move — does the market price reflect this?"

### Solution

The window start timestamp is embedded in the market slug:

```
btc-updown-5m-1710000000
                └── unix timestamp = window start
```

So `window_start_ts = int(slug.rsplit("-", 1)[1])` and `window_end_ts = window_start_ts + 300`.

**For backtesting:** Binary search the recorded spot prices for the nearest timestamp to `window_start_ts`.

**For paper trading:** When MarketEngine discovers a new market, look up the current spot price at that moment. Or use the prices-history API: `GET /prices-history?market={asset_id}&startTs={window_start_ts}&endTs={window_start_ts+60}&interval=1m`

### Fields Added to MarketState

```python
@dataclass
class MarketState:
    # ... existing fields ...
    window_start_ts: int          # unix seconds
    window_end_ts: int            # window_start_ts + 300
    elapsed_sec: int              # seconds into window
    remaining_sec: int            # seconds until resolution
    spot_price_at_window_start: float
    spot_return_bps: float        # (current - start) / start * 10000
```

These are computed by the `StrategyRunner` when building state, not by the strategy.

---

## 4. Risk Limits

### Design Principle

All limits are **config values in a dataclass**, not hardcoded. This makes them:
- **Testable** — sweep parameters in backtests to find optimal values
- **Dynamic** — can be adjusted between runs or even mid-run
- **Transparent** — every blocked trade logs the reason

### RiskConfig

```python
@dataclass
class RiskConfig:
    # Position limits
    max_position_per_market: int = 500       # max contracts in a single market
    max_total_exposure: float = 5000.0       # max total capital at risk across all positions
    max_concurrent_positions: int = 6        # max open positions at once

    # Loss limits
    max_loss_per_window: float = 200.0       # circuit breaker: max loss in a single 5-min window
    max_drawdown_pct: float = 10.0           # circuit breaker: % of bankroll

    # Market quality filters
    min_spread_bps: int = 0                  # 0 = no minimum (trade any spread)
    max_spread_bps: int = 500                # don't enter if spread > 5 cents
    min_liquidity: float = 0.0              # minimum book depth (in dollars)

    # Timing
    min_remaining_sec: int = 30              # don't enter < 30s before expiry
    max_elapsed_sec: int = 240               # don't enter after 4 min into window

    # Recovery
    cooldown_after_loss_sec: int = 0         # pause N seconds after a losing trade (0 = none)
```

### RiskEngine

```python
class RiskEngine:
    def check(self, signal: Signal, state: MarketState, portfolio: Portfolio) -> tuple[bool, str]:
        """Returns (allowed, reason_if_blocked)."""
        # Check each limit, return first failure
```

### Parameter Sweeping in Backtests

Run the same strategy with different risk configs:

```python
configs = [
    RiskConfig(max_spread_bps=200, max_concurrent_positions=3),
    RiskConfig(max_spread_bps=500, max_concurrent_positions=6),
    RiskConfig(max_spread_bps=300, max_drawdown_pct=5.0),
]
for cfg in configs:
    result = run_backtest(strategy, data, risk_config=cfg)
    print(f"{cfg} -> net_pnl={result.net_pnl}, trades={result.trade_count}")
```

---

## 5. Data Collection — VPS Recorder + Chainlink

### Architecture

24/7 VPS recorder captures 5 concurrent streams into daily JSONL files:

| Stream | Source | Frequency | Data |
|--------|--------|-----------|------|
| Spot prices | Coinbase WS | Sub-second | BTC/ETH/SOL tick prices |
| Oracle prices | Chainlink on-chain (Polygon) | Every 5s | BTC/ETH/SOL Chainlink prices |
| CLOB | Polymarket WS | Real-time | Order book updates, trades, price changes |
| Market discovery | Polymarket Gamma REST | Every 60s | New 5-min markets, condition IDs |
| Resolution | Polymarket REST | Every 30s | Outcome detection (YES/NO) |

### Chainlink vs Coinbase — Resolution Source

**Polymarket resolves via Chainlink Data Streams, NOT Coinbase.** The ~0.3 bps difference between exchanges can flip outcomes on tight (<5 bps) moves. Our recorder collects both.

Chainlink Price Feed contracts (Polygon mainnet):
- BTC/USD: `0xc907E116054Ad103354f2D350FD2514433D57F6f`
- ETH/USD: `0xF9680D99D6C9589e2a93a78A04A279e509205945`
- SOL/USD: `0x10C8264C0935b3B9870013e057f330Ff3e9C56dC`

### Polymarket RTDS — Two Price Feeds

Polymarket's Real-Time Data Socket exposes two separate price channels:
- `crypto_prices` — sourced from **Binance** (what traders see and trade off)
- `crypto_prices_chainlink` — sourced from **Chainlink** (what actually resolves)

This divergence is exploitable: the market prices off Binance, but resolution uses Chainlink.

### Deployment

VPS: `root@143.110.129.50` (user: openclaw), running as systemd service `polyapi-collector`. See `collector/DEPLOY.md` for full setup guide.

### Data Sync

```powershell
.\collector\sync_data.ps1                    # download from VPS
python -m collector.replay data_store/ --all  # replay through all strategies
```

### `/prices-history` Endpoint

```
GET https://clob.polymarket.com/prices-history?market={asset_id}&startTs={unix}&endTs={unix}&fidelity=1
Response: {"history": [{"t": 1710000000, "p": 0.52}, ...]}
```

---

## 6. Paper Trading Engine

### What It Is

A simulated execution environment that processes live market data through the same strategy pipeline as backtesting, but uses real-time order book snapshots for fill decisions instead of historical data.

### Accuracy vs Real Execution

| Simulation Quality | Error vs Reality | Build Effort |
|---|---|---|
| Midpoint fills (naive) | 50-200+ bps | Trivial |
| **L2 book walk (target)** | **10-30 bps** | **~100 lines** |
| L2 + latency model | 5-15 bps | More complex |

### Sources of Error

| Error | Impact | Mitigation |
|-------|--------|------------|
| **Book staleness** | 5-15 bps | Fetch fresh book before each fill decision |
| **No market impact** | 5-10 bps | Conservative fill ratio (assume you only fill 80% of shown depth) |
| **Queue position** | 0-20 bps | Only model taker fills (FOK/FAK), not maker |
| **Fee model error** | 0-5 bps | Use exact Polymarket fee curve (see Section 1) |

### Existing Open-Source References

| Tool | What to Learn From It |
|------|----------------------|
| [polymarket-paper-trader](https://github.com/agent-next/polymarket-paper-trader) | L2 book walking, exact fee model, slippage tracking |
| [prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting) | Event-driven architecture on NautilusTrader |

### Fill Simulator Design

```python
def walk_book(levels: list[tuple[float, float]], target_size: float) -> tuple[float, float, float]:
    """
    Walk order book levels to simulate a fill.
    levels: [(price, size), ...] sorted best-to-worst
    Returns: (avg_fill_price, filled_size, unfilled_size)
    """
    remaining = float(target_size)
    filled = notional = 0.0
    for price, size in levels:
        take = min(remaining, float(size))
        notional += take * float(price)
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    avg_fill = notional / filled if filled else 0.0
    return avg_fill, filled, max(0.0, target_size - filled)
```

For paper trading, fetch the live book from Polymarket before each fill:
```
GET https://clob.polymarket.com/book?token_id={yes_token_id}
→ {bids: [{price, size}, ...], asks: [{price, size}, ...], ...}
```

### Key Principle

**If a strategy can't survive 30 bps of simulation error, it doesn't have enough edge to trade live.** Don't over-engineer the simulator. Build the L2 book walker, plug in the fee curve, and test.

---

## 7. CLOB Matching Engine (Reference)

### How Polymarket's CLOB Works

- **Off-chain orderbook** — operator matches orders, users submit signed messages (EIP-712)
- **On-chain settlement** — Polygon smart contract executes atomic swaps
- **Price-time priority** — standard CLOB behavior

### Order Types

| Type | Behavior | Partial Fills |
|------|----------|--------------|
| GTC (Good-Til-Cancelled) | Rests on book until filled/cancelled | Yes |
| GTD (Good-Til-Date) | Active until specified timestamp | Yes |
| FOK (Fill-Or-Kill) | Fill entirely now or cancel | No |
| FAK (Fill-And-Kill) | Fill what's available now, cancel rest | Yes |

### Three Execution Modes

1. **Direct match** — buyer meets seller at same price
2. **Minting** — YES buyer + NO buyer sum to $1 → new tokens minted
3. **Merging** — opposite sell orders → tokens burned, collateral returned

### Tick Sizes

Market-specific: `0.1`, `0.01`, `0.001`, or `0.0001`

---

## 8. Exploitable Edges (Research, March 2026)

### 8.1 "Flat = YES" Structural Bias

Resolution rule: "Up" if end price **>= start price**. Flat counts as YES. Adds ~0.25% to true P(YES), making fair value ~50.25% not 50.00%. Too small alone (eaten by fees at midpoint), but use as **tiebreaker when models are ambiguous — always lean YES**.

### 8.2 Turn-of-Candle Effect (t-stat > 9)

Academically documented: positive returns of +0.58 bps/min concentrate at minutes 0, 15, 30, 45 of each hour. Windows ending on 15-minute boundaries have a slight YES bias from algorithmic trading triggered by candle closes.

Source: [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10015199/), [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S2405844023014433)

### 8.3 Binance-Chainlink Divergence

Polymarket displays Binance prices (`crypto_prices` RTDS channel) but resolves on Chainlink (`crypto_prices_chainlink`). Traders price off Binance; resolution uses Chainlink. When Binance shows +3 bps but Chainlink shows flat, the market is systematically mispriced. Most valuable when spot return is near zero (<5 bps).

### 8.4 Volatility Regime Filter

5-minute crypto returns are NOT normally distributed — heavy tails, negative skew, volatility clustering. In low-vol periods, direction is nearly random and prices stay near 50c where fees are 1.56%. **Must skip low-vol windows.** In high-vol periods, prices move to extremes where fees are 4x lower.

### 8.5 Fee Dead Zone Avoidance

Trading at prices 0.45-0.55 requires >156 bps of edge just to break even on fees. Most strategies can't produce this. Only trade when prices are at extremes (<0.25 or >0.75) where fees approach zero, OR when divergence is very large (>200 bps).

### 8.6 Last-30-Second Sniping

By T-30s, direction is ~80-85% determined. Polymarket price lags because liquidity thins out. Late-window entry captures the widest divergence between model prediction and stale market price. Combined with the fee advantage at extremes (prices have moved away from 50c by then).

### 8.7 Maker Rebates — Zero Fee + Rebate

Makers pay ZERO fees and receive 20% of taker fees as a rebate. Switching from taker to maker flips a 1.56% headwind into a ~0.3% tailwind. Implementation is harder (limit orders, fill risk in 5-min window) but the largest single edge available.

Source: [Polymarket Maker Rebates](https://docs.polymarket.com/polymarket-learn/trading/maker-rebates-program)

### 8.8 Speed Bump Removal (March 2026)

Polymarket removed its 500ms speed bump. Pure arbitrage (YES+NO < $1) now requires sub-100ms execution, with 73% of arb profits captured by sub-100ms bots. The profitable frontier has shifted to **directional prediction with structural advantages** — our approach.

Source: [Protos](https://protos.com/polymarket-ends-trading-loophole-for-bitcoin-quants/)

### 8.9 Cross-Exchange Lead-Lag

Binance and Coinbase lead smaller exchanges by 1-4 seconds. Coinbase may lead Chainlink by 0.2-1.4s during rapid moves. Practical edge window is 200-800ms — matters most in final 30 seconds when resolution price is being sampled.

### 8.10 Order Flow Signals

- **VPIN** predicts future price jumps with positive serial correlation
- **OBI** has 2-5% R² for short-term return prediction
- **Spread widening** signals MM uncertainty — expect a move
- OBI signals are **most predictive in the final 60 seconds** when liquidity providers step back

### 8.11 Fee Reframing — Win Rate Is King

At 50c, if you're right you win **$0.50 per contract** (100% return). The 1.56% fee is $0.008 — noise compared to the $0.50 payoff. Fee drag only matters when edge is tiny and sizing is small.

**The real question is: can we predict direction with >50% accuracy?** If yes, trading at 50c is the BEST place because the payoff is largest. The bar is >50% accuracy, not >51.56%.

Priority:
1. **Win rate is king** — focus on prediction accuracy, not fee avoidance
2. **Big spreads = big payoffs** — 50c markets offer the highest upside per contract
3. **Still need a real signal** — but confidence in direction matters more than fee optimization
4. **Vol regime filter still matters** — not to avoid fees, but to avoid truly random periods where accuracy drops below 50%

This shifts the strategy focus from fee-extremes hunting to **Brownian model calibration + vol regime filtering + Chainlink alignment**.

### Why Current Strategies Lose

1. Use Coinbase prices but resolve on Chainlink — systematic prediction error
2. No vol regime filter — trade random noise in low-vol periods where accuracy < 50%
3. Brownian model sigma miscalibrated — need to tune from real data
4. Trading too early (30-180s) — not enough signal to predict accurately
5. Quant_models Brownian alone bets NO too aggressively on tiny negative returns

---

## References

- [Polymarket Fees](https://docs.polymarket.com/trading/fees)
- [Polymarket Order Book](https://docs.polymarket.com/trading/orderbook)
- [Polymarket Resolution](https://docs.polymarket.com/concepts/resolution)
- [Polymarket Prices History API](https://docs.polymarket.com/developers/CLOB/timeseries)
- [Polymarket Order Book API](https://docs.polymarket.com/api-reference/market-data/get-order-book)
- [polymarket-paper-trader](https://github.com/agent-next/polymarket-paper-trader)
- [prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting)
- [How to Backtest Polymarket Strategies](https://polymarketdata.co/blog/how-to-backtest-polymarket-strategies-python)
- [Polymarket Fee Curve Analysis](https://quantjourney.substack.com/p/understanding-the-polymarket-fee)

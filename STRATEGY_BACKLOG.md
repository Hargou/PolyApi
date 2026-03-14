# Strategy Research Backlog

**Core thesis:** Use quant models on actual crypto spot prices to predict 5-min direction, then exploit discrepancies where Polymarket misprices the outcome.

**Key insight from backtesting:** The current `spot_momentum` strategy loses money (-$1007, 23% win rate). `always_yes` made $138 because BTC trended up -- that's bias, not edge. We need real edge from crypto price modeling.

---

## Critical Finding: Fee Structure Defines Everything

The non-linear fee `fee = size * price * 0.25 * (p * (1-p))^2` creates three regimes:

| Zone | Price Range | Eff. Fee Rate | Break-Even Edge | Strategy |
|------|------------|---------------|-----------------|----------|
| **Dead Zone** | 0.35 - 0.65 | 1.0 - 1.56% | 100 - 156 bps | Only very strong signals |
| **Moderate** | 0.20 - 0.35 / 0.65 - 0.80 | 0.6 - 1.3% | 60 - 130 bps | Moderate signals work |
| **Opportunity** | < 0.20 / > 0.80 | < 0.6% | < 60 bps | Even weak signals profit |

**Implication:** Don't fight the fee structure. Trade at extremes where fees are near-zero, or use very strong signals when forced to trade near 50%.

---

## Strategy Priority (Tier 1 = implement first)

### Tier 1 — High confidence, exploits structural edge

| # | Strategy | Core Idea | Why Priority |
|---|----------|-----------|--------------|
| 1 | **FeeOptimizedExtremes** | Only trade when market price is extreme (< 0.20 or > 0.80). Fee drag drops 4x. Confirm or fade with spot model. | Structural fee advantage. Simple. |
| 2 | **TimeDecayBias** | Late in window (> 150s elapsed), small spot returns become very predictive because less time for reversals. Use `P(up) = Phi(return / (sigma * sqrt(remaining)))`. | Closed-form math from Brownian motion. Exploits behavioral anchoring at 50%. |
| 3 | **SpotPredictionDivergence** | Compute fair P(up) from spot return, compare to Polymarket midpoint. Trade the gap. Time-weight the divergence (stronger later in window). | Core thesis: spot market is smarter than prediction market. |

### Tier 2 — Microstructure signals, needs book data

| # | Strategy | Core Idea | Why Priority |
|---|----------|-----------|--------------|
| 4 | **OrderBookImbalance** | `OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)`. Persistent OBI reveals informed flow. | Academic backing (2-5% R^2). Needs EMA tracking. |
| 5 | **DepthWeightedFairValue** | Microprice: `(bid * ask_size + ask * bid_size) / (bid_size + ask_size)`. Better fair value than midpoint. | Simple math, uses existing book data. |
| 6 | **LiquidityVacuum** | When one book side thins out, MMs are stepping away = they expect a move. `log(bid_depth / ask_depth)` as signal. | Market maker behavior leaks information. |

### Tier 3 — Multi-signal / cross-asset

| # | Strategy | Core Idea | Why Priority |
|---|----------|-----------|--------------|
| 7 | **CrossAssetCorrelation** | BTC leads ETH/SOL by 1-5 seconds. If BTC spikes but ETH market hasn't repriced, buy ETH YES. | Real edge but needs cross-asset state in runner. |
| 8 | **SpotBookConsensus** | Only trade when spot direction, OBI, AND price movement all agree. High win rate, fewer trades. | Ensemble filter. Needs strategies 3+4 first. |
| 9 | **VolatilityRegime** | Estimate intra-window vol. Skip low-vol windows (fee > edge). Size up in high-vol (prices at extremes). | Prevents bad trades. Meta-strategy. |

### Tier 4 — Advanced / research

| # | Strategy | Core Idea | Why Priority |
|---|----------|-----------|--------------|
| 10 | **SpreadCompression** | Rapid spread tightening = MM conviction. Direction of midpoint shift during compression reveals outcome. | Needs tick-level spread tracking. |
| 11 | **MomentumAcceleration** | Second derivative of spot return (acceleration) predicts continuation better than level alone. | Noisy on thin data. Needs smoothing. |
| 12 | **ContrarianExtremeFade** | When midpoint > 0.80 early (< 90s), it's often an overreaction. Fade with cheap NO contracts. | Contrarian -- high risk of trend continuation. |

---

## Implementation Plan

### Phase A: Fix infrastructure (required for all strategies)
- [ ] **A.1** Fix synthetic backtest data -- current data derives spot FROM market probability (circular). Need independent spot price data.
- [ ] **A.2** Extend `MarketState` with raw book levels (`bids`, `asks`, `bid_size_at_best`, `ask_size_at_best`)
- [ ] **A.3** Add `microprice` computed field to `MarketState`
- [ ] **A.4** Support cross-asset spot returns in `MarketState` (`other_spot_returns: Dict[str, float]`)
- [ ] **A.5** Extend `cli/sweep.py` to sweep strategy params (not just risk params)

### Phase B: Tier 1 strategies
- [ ] **B.1** Implement `FeeOptimizedExtremesStrategy` (skeleton exists in `research/strategy_proposals.py`)
- [ ] **B.2** Implement `TimeDecayBiasStrategy` (closed-form Phi model)
- [ ] **B.3** Implement `SpotPredictionDivergenceStrategy` (time-weighted)
- [ ] **B.4** Fix `SpotMomentumStrategy` -- recalibrate logistic scale, add fee-aware Kelly
- [ ] **B.5** Backtest all Tier 1 strategies, compare to benchmarks

### Phase C: Tier 2 strategies (orderbook)
- [ ] **C.1** Implement `OrderBookImbalanceStrategy` (EMA-smoothed OBI)
- [ ] **C.2** Implement `DepthWeightedFairValueStrategy` (microprice)
- [ ] **C.3** Implement `LiquidityVacuumStrategy`
- [ ] **C.4** Backtest, compare, identify which book signals add value

### Phase D: Tier 3 strategies (ensemble/cross-asset)
- [ ] **D.1** Implement `CrossAssetCorrelationStrategy` (requires A.4)
- [ ] **D.2** Implement `SpotBookConsensusStrategy` (multi-signal filter)
- [ ] **D.3** Implement `VolatilityRegimeStrategy` (meta-filter)
- [ ] **D.4** Build ensemble framework: strategy rotation or signal averaging

---

## Key Mathematical Models

### 1. Brownian Motion P(up) -- closed form
```
P(up | return_t, remaining, sigma) = Phi(return_t / (sigma * sqrt(remaining)))
```
Better than logistic for time-varying probability. Requires vol estimate.

### 2. Fee-Aware Kelly
```
effective_cost = price + price * 0.25 * (price * (1-price))^2
effective_profit = 1.0 - effective_cost
f* = (p_hat * profit - (1-p_hat) * cost) / profit * fraction
```

### 3. Microprice
```
microprice = (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)
```

### 4. Cross-Asset Lead-Lag
```
E[R_ETH | R_BTC = r] = rho * (sigma_ETH / sigma_BTC) * r
```
BTC-ETH rho ~ 0.85, BTC-SOL rho ~ 0.75

---

## Data Requirements

- **Minimum:** 300+ windows per asset for meaningful backtest
- **Calibration:** Logistic regression on (spot_return, elapsed, outcome) to find optimal scale
- **Vol estimation:** Need tick-level spot prices within each window (have this from Coinbase WS)
- **Storage:** ~70MB/day if recording all ticks

---

## Full research document: `research/strategy_proposals.py` (2,262 lines)
Contains all derivations, implementation skeletons, parameter sweep plans, ensemble framework, and risk analysis.

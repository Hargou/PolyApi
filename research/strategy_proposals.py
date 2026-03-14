"""
================================================================================
STRATEGY RESEARCH: Polymarket 5-Minute Crypto Binary Prediction Markets
================================================================================
Date: 2026-03-14
Author: Automated research pipeline

Target: BTC/ETH/SOL "Will X go up?" markets, 5-minute windows.
Resolves YES if asset price at T+300s > price at T+0s, else NO.

Existing strategy: SpotMomentumStrategy (Bayesian logistic of spot return, Kelly sizing)

================================================================================
TABLE OF CONTENTS
================================================================================
    PART I:    Fee Model Deep Dive & Structural Analysis
    PART II:   Strategy Proposals (12 strategies)
    PART III:  Detailed Mathematical Derivations
    PART IV:   MarketState Extension Specification
    PART V:    Concrete Implementation Skeletons (Tier 1 strategies)
    PART VI:   Parameter Sweep Plans & Backtesting Methodology
    PART VII:  Ensemble / Strategy Combination Framework
    PART VIII: Risk Considerations & Failure Modes
    PART IX:   Data Requirements & Collection Plan

================================================================================
PART I: FEE MODEL DEEP DIVE & STRUCTURAL ANALYSIS
================================================================================

Fee formula:
    fee = size * price * 0.25 * (price * (1 - price))^2

Expanding:
    fee = size * 0.25 * price * p^2 * (1-p)^2
        = size * 0.25 * p^3 * (1-p)^2     (where p = price)

Effective fee rate (per dollar of notional = size * price):
    eff_rate = 0.25 * (p * (1-p))^2

This is a quartic function that peaks at p = 0.50:
    eff_rate(0.50) = 0.25 * (0.25)^2 = 0.25 * 0.0625 = 0.015625 = 1.5625%

And drops to near-zero at extremes:
    eff_rate(0.10) = 0.25 * (0.09)^2 = 0.25 * 0.0081 = 0.002025 = 0.0164%
    (Wait, 0.10 * 0.90 = 0.09, 0.09^2 = 0.0081, * 0.25 = 0.002025 -> 0.20%)
    Let me recompute properly:
    eff_rate(0.10) = 0.25 * (0.10 * 0.90)^2 = 0.25 * 0.09^2 = 0.25 * 0.0081 = 0.002025
    As percentage: 0.2025%

    eff_rate(0.15) = 0.25 * (0.15 * 0.85)^2 = 0.25 * 0.1275^2 = 0.25 * 0.01626 = 0.004066
    As percentage: 0.4066%

    eff_rate(0.20) = 0.25 * (0.20 * 0.80)^2 = 0.25 * 0.16^2 = 0.25 * 0.0256 = 0.0064
    As percentage: 0.64%

    eff_rate(0.30) = 0.25 * (0.30 * 0.70)^2 = 0.25 * 0.21^2 = 0.25 * 0.0441 = 0.011025
    As percentage: 1.1025%

Corrected fee reference table:

    Price  | p*(1-p) | eff_rate  | Fee per $100 notional | Break-even edge (bps)
    -------|---------|-----------|----------------------|---------------------
    0.05   | 0.0475  | 0.0564%   | $0.056               |  5.6
    0.10   | 0.0900  | 0.2025%   | $0.203               | 20.3
    0.15   | 0.1275  | 0.4066%   | $0.407               | 40.7
    0.20   | 0.1600  | 0.6400%   | $0.640               | 64.0
    0.25   | 0.1875  | 0.8789%   | $0.879               | 87.9
    0.30   | 0.2100  | 1.1025%   | $1.103               | 110.3
    0.35   | 0.2275  | 1.2935%   | $1.294               | 129.4
    0.40   | 0.2400  | 1.4400%   | $1.440               | 144.0
    0.45   | 0.2475  | 1.5314%   | $1.531               | 153.1
    0.50   | 0.2500  | 1.5625%   | $1.563               | 156.3

    (Symmetric: eff_rate(p) = eff_rate(1-p))

HOLD-TO-EXPIRY EV ANALYSIS:

    With hold-to-expiry, there is NO exit fee (exit at 1.0 or 0.0).
    Only the entry taker fee matters.

    For buying YES at price p:
        Cost = size * p + fee(p, size)
        Win payout = size * 1.0 (if outcome = YES)
        Lose payout = 0.0 (if outcome = NO)
        EV = p_true * size - size * p - fee(p, size)
           = size * (p_true - p) - fee(p, size)
           = size * (p_true - p - eff_rate * p)
           = size * (p_true - p * (1 + eff_rate))

    For buying NO at price (1-p):
        Cost = size * (1-p) + fee(1-p, size)
        Win payout = size * 1.0 (if outcome = NO)
        EV = (1-p_true) * size - size * (1-p) - fee(1-p, size)
           = size * (p - p_true) - fee(1-p, size)

    Break-even: p_true = p * (1 + eff_rate(p))  [for YES side]
    At p=0.50: p_true must be > 0.50 * 1.015625 = 0.50781 -> need 78 bps edge
    At p=0.15: p_true must be > 0.15 * 1.004066 = 0.15061 -> need 6 bps edge
    At p=0.85: buying YES costs 0.85, need p_true > 0.85 * 1.004066 = 0.85346

STRUCTURAL CONCLUSION:
    The fee structure creates three distinct trading regimes:
    1. DEAD ZONE (p in [0.35, 0.65]): fees are 1.0-1.6%, need 100-156 bps edge.
       Only very strong signals can overcome this.
    2. MODERATE ZONE (p in [0.20, 0.35] or [0.65, 0.80]): fees 0.6-1.3%.
       Moderate signals can work here.
    3. OPPORTUNITY ZONE (p < 0.20 or p > 0.80): fees < 0.6%.
       Even weak signals are profitable. This is where we want to trade.

    Strategy implication: We should NOT fight the fee structure. Instead of
    trading at p=0.50 with a strong signal, WAIT for the market to move to
    an extreme and trade there. The information loss from waiting is typically
    less than the fee savings.


================================================================================
PART II: STRATEGY PROPOSALS (12 strategies)
================================================================================

--------------------------------------------------------------------------------
STRATEGY 1: Order Book Imbalance (OBI)
--------------------------------------------------------------------------------

Name: OrderBookImbalance

Core hypothesis:
    Persistent asymmetry in bid vs ask depth on the prediction market reveals
    informed flow direction. When bid_depth >> ask_depth, participants with
    superior information are accumulating YES, predicting the crypto asset
    will rise. This orderbook imbalance is a leading indicator of outcome.

Signal construction:
    OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)
    OBI ranges from -1 (all asks) to +1 (all bids).

    Smoothed OBI: exponential moving average over recent ticks within the window.
    OBI_ema(t) = alpha * OBI(t) + (1 - alpha) * OBI_ema(t-1), alpha = 0.3

    Map to probability adjustment:
    p_hat = midpoint + beta * OBI_ema
    where beta is a tunable sensitivity parameter (e.g., 0.10).
    Clamp p_hat to [0.02, 0.98].

Entry conditions:
    - abs(OBI_ema) > obi_threshold (e.g., 0.25)
    - elapsed_sec in [30, 180]
    - spread_bps < max_spread
    - edge = abs(p_hat - midpoint) * 10000 > min_edge_bps

Expected edge source:
    Informed traders place limit orders ahead of expected moves. Market makers
    may be slower to update quotes on Polymarket's CLOB than spot moves.
    The imbalance signal captures this information asymmetry.

Key tunable parameters:
    - obi_threshold: minimum OBI magnitude to trigger (0.15 - 0.40)
    - beta: OBI-to-probability sensitivity (0.05 - 0.20)
    - alpha: EMA smoothing factor (0.1 - 0.5)
    - min_edge_bps: minimum required edge (100 - 400)
    - kelly_fraction: fractional Kelly (0.10 - 0.50)

Risk/weakness:
    - OBI can be spoofed: large resting orders pulled before fill.
    - Low depth on both sides makes OBI noisy (division by small numbers).
    - If market makers are symmetric, OBI may just reflect temporary noise.
    - Need to track multiple snapshots per window (requires internal state).

--------------------------------------------------------------------------------
STRATEGY 2: Spread Compression Anticipator
--------------------------------------------------------------------------------

Name: SpreadCompression

Core hypothesis:
    In the first minute of a 5-minute window, wide spreads reflect uncertainty.
    When spreads rapidly compress (tighten), it signals that market makers have
    gained conviction about the outcome. The direction of the midpoint shift
    during compression reveals informed consensus.

Signal construction:
    Track spread_bps over time within each window.
    spread_velocity = (spread_bps(t) - spread_bps(t - k)) / k  (bps per second)
    midpoint_drift = midpoint(t) - midpoint(t_0)  (change from first observation)

    Signal fires when:
    1. spread_velocity < -threshold (spread tightening rapidly)
    2. abs(midpoint_drift) > drift_threshold

    p_hat = midpoint + sign(midpoint_drift) * confidence_boost
    where confidence_boost = min(abs(midpoint_drift) * amplifier, 0.15)

Entry conditions:
    - spread_velocity < -spread_velocity_threshold (e.g., -5 bps/sec)
    - midpoint_drift magnitude > min_drift (e.g., 0.02)
    - elapsed_sec in [20, 120] (early in window, before info fully priced)
    - spread_bps < 300 (spread has already compressed enough to trade)

Expected edge source:
    Market maker behavior: when they narrow quotes, they are expressing
    conviction. The midpoint moves towards the true outcome before the
    wider market recognizes it. Early entry during compression captures
    this transition.

Key tunable parameters:
    - spread_velocity_threshold: how fast spread must be tightening (-2 to -10 bps/s)
    - drift_threshold: minimum midpoint drift to trigger (0.01 - 0.05)
    - amplifier: how much to boost probability estimate (1.0 - 3.0)
    - confidence_boost_cap: max probability adjustment (0.05 - 0.20)
    - lookback_ticks: how many ticks to measure velocity over (3 - 10)

Risk/weakness:
    - Spreads can tighten without directional conviction (just more liquidity).
    - Late entry after compression means the edge may already be priced in.
    - Requires enough tick frequency to measure velocity accurately.

--------------------------------------------------------------------------------
STRATEGY 3: Spot-Prediction Divergence (Mean Reversion)
--------------------------------------------------------------------------------

Name: SpotPredictionDivergence

Core hypothesis:
    When the prediction market midpoint diverges significantly from the
    "fair" probability implied by spot price movement, one of the two is
    wrong. Since the spot market (Coinbase) has deeper liquidity and more
    sophisticated participants, the spot-implied probability is more likely
    correct. The prediction market will converge to spot-implied fair value.

Signal construction:
    Compute spot-implied probability using a calibrated model:
    p_spot = logistic(spot_return_bps / scale_factor)
    where scale_factor is calibrated from historical data (e.g., 200).

    Divergence = p_spot - midpoint

    If divergence > threshold: buy YES (market is underpricing spot signal)
    If divergence < -threshold: buy NO (market is overpricing relative to spot)

    Time-weight the divergence: the signal is stronger when elapsed_sec is
    larger (more of the spot return is "locked in"):
    time_weight = min(elapsed_sec / 120, 1.0)
    adjusted_divergence = divergence * time_weight

Entry conditions:
    - abs(adjusted_divergence) > min_divergence (e.g., 0.04)
    - elapsed_sec in [45, 200]
    - spread_bps < max_spread
    - spot_return_bps magnitude > 5 (non-trivial move has occurred)

Expected edge source:
    Prediction market participants react slowly to spot price changes.
    Retail-heavy Polymarket may lag the highly efficient Coinbase spot market.
    This is essentially an arbitrage between two correlated instruments.

Key tunable parameters:
    - scale_factor: logistic scale for spot return (100 - 500)
    - min_divergence: minimum divergence to trade (0.02 - 0.08)
    - time_weight_ramp: how quickly time weight reaches 1.0 (60 - 180 sec)
    - kelly_fraction: (0.15 - 0.50)
    - max_spread_bps: (200 - 500)

Risk/weakness:
    - The spot-implied model may be miscalibrated (wrong scale_factor).
    - Spot can reverse in remaining time, making the "locked in" assumption wrong.
    - If prediction market participants have additional info (e.g., about
      upcoming news), divergence might be the prediction market being RIGHT.
    - Very similar to existing SpotMomentumStrategy; differentiation is the
      explicit divergence framing and time-weighting.

--------------------------------------------------------------------------------
STRATEGY 4: Volatility Regime Filter
--------------------------------------------------------------------------------

Name: VolatilityRegime

Core hypothesis:
    The profitability of directional strategies varies dramatically with
    realized volatility. In low-vol regimes, spot barely moves and markets
    price ~50/50. Any edge from momentum is erased by fees. In high-vol
    regimes, spot makes large moves and prediction market probabilities
    diverge from 50%, creating exploitable mispricings with lower fee drag
    (prices far from 0.50 have minimal fees).

Signal construction:
    Track intra-window spot price volatility:
    vol_realized = std_dev(spot_return_bps over last N ticks within window)

    Regime classification:
    - LOW: vol_realized < vol_low_threshold    -> DO NOT TRADE
    - MEDIUM: between thresholds               -> trade with base sizing
    - HIGH: vol_realized > vol_high_threshold   -> trade with boosted sizing

    Underlying directional signal: same as SpotMomentum logistic.
    p_hat = logistic(spot_return_bps / (logistic_scale * vol_adjustment))
    where vol_adjustment = max(vol_realized / vol_baseline, 0.5) to normalize
    the logistic sensitivity by current volatility.

    Size multiplier:
    if HIGH regime: size *= 1.5 (more conviction, larger moves)
    if LOW regime: size = 0 (don't trade)

Entry conditions:
    - vol_realized > vol_low_threshold (skip low-vol windows)
    - Base directional signal has edge > min_edge_bps
    - elapsed_sec in [60, 180] (need enough ticks to estimate vol)
    - spread_bps < max_spread

Expected edge source:
    Filtering out low-vol windows eliminates trades where fee drag exceeds
    expected edge. In high-vol windows, spot moves are large enough that
    prediction market prices move to extremes (0.20 or 0.80+), where fees
    are negligible and directional accuracy becomes the dominant factor.

Key tunable parameters:
    - vol_low_threshold: below this, don't trade (5 - 20 bps std)
    - vol_high_threshold: above this, use boosted sizing (30 - 60 bps std)
    - vol_baseline: normalization constant for logistic (20 - 40 bps)
    - high_vol_size_multiplier: (1.2 - 2.0)
    - min_ticks_for_vol: minimum observations before vol estimate is valid (10 - 30)

Risk/weakness:
    - Need sufficient tick frequency within a 5-minute window to estimate vol.
    - Realized vol is backward-looking; regime can change within the window.
    - In extreme vol, spot reversals are also larger, potentially increasing loss.
    - Requires internal state to track tick-by-tick spot prices per window.

--------------------------------------------------------------------------------
STRATEGY 5: Depth-Weighted Fair Value (Microprice)
--------------------------------------------------------------------------------

Name: DepthWeightedFairValue

Core hypothesis:
    The midpoint (best_bid + best_ask) / 2 is a naive estimate of fair value.
    A better estimate weights by available liquidity at each level, since the
    book side with less depth will be consumed faster, shifting the price.
    The depth-weighted microprice is a superior fair value estimate.

Signal construction:
    Standard microprice formula:
    microprice = (best_bid * ask_size_at_best + best_ask * bid_size_at_best)
                 / (bid_size_at_best + ask_size_at_best)

    If bid_size >> ask_size at the inside, microprice shifts toward best_ask
    (because buyers are more aggressive -> fair value is higher).

    Then:
    p_hat = microprice (since price = probability in binary markets)
    edge = p_hat - midpoint (or midpoint - p_hat for NO side)

    Combine with spot signal:
    p_combined = w_micro * microprice + w_spot * p_spot + (1 - w_micro - w_spot) * midpoint

Entry conditions:
    - abs(microprice - midpoint) > min_microprice_edge (e.g., 0.015)
    - bid_depth + ask_depth > min_total_depth (need enough depth for signal validity)
    - spread_bps < max_spread
    - elapsed_sec in [20, 200]

Expected edge source:
    The microprice incorporates information from the shape of the book that
    the simple midpoint ignores. In a thin CLOB like Polymarket, the inside
    sizes carry significant information about where fair value sits.

Key tunable parameters:
    - w_micro: weight on microprice in combined estimate (0.3 - 0.7)
    - w_spot: weight on spot-implied probability (0.2 - 0.5)
    - min_microprice_edge: minimum microprice vs midpoint divergence (0.005 - 0.03)
    - min_total_depth: minimum book depth for signal validity ($50 - $500)
    - use_full_depth: whether to use total depth or only best-level sizes

Risk/weakness:
    - Polymarket books may be too thin for microprice to be informative.
    - Spoofing: large resting orders at the inside to manipulate microprice.
    - Microprice is a short-term predictor; may not predict 5-min outcome.
    - Need access to per-level sizes (bids/asks arrays), which requires
      extending MarketState or using raw ClobSnapshot data in strategy.

--------------------------------------------------------------------------------
STRATEGY 6: Cross-Asset Correlation
--------------------------------------------------------------------------------

Name: CrossAssetCorrelation

Core hypothesis:
    BTC, ETH, and SOL are highly correlated in short timeframes. When one
    asset makes a sharp move, the others tend to follow within seconds.
    If BTC spikes up but ETH's prediction market hasn't repriced, buying
    ETH YES captures the lagged correlation.

Signal construction:
    For asset X's market, compute leader signals from other assets:

    leader_signal = sum(w_i * spot_return_bps_i) for i != X

    where weights are empirical correlations:
    w_BTC = 1.0 (BTC leads), w_ETH = 0.7, w_SOL = 0.5
    (These are rough; calibrate from data.)

    cross_implied_p = logistic(leader_signal / cross_scale)

    Divergence = cross_implied_p - midpoint_X

    If the target asset's spot hasn't moved yet but correlated assets have,
    this signals an upcoming move.

    Composite signal:
    p_hat = w_own * p_spot_own + w_cross * cross_implied_p + (1 - w_own - w_cross) * midpoint

Entry conditions:
    - Leader assets have moved significantly: abs(leader_signal) > min_leader_bps
    - Target asset's own spot has NOT yet moved much: abs(own_spot_return) < max_own_move
    - elapsed_sec in [10, 120] (early; before the correlation propagates)
    - Divergence > min_edge_bps

Expected edge source:
    Cross-asset information propagates with a lag. BTC often leads ETH/SOL
    by 1-5 seconds in microstructure. The prediction market for the follower
    asset may lag even more (10-30 seconds). This window of mispricing is
    exploitable.

Key tunable parameters:
    - w_own: weight on target asset's own spot (0.3 - 0.6)
    - w_cross: weight on cross-asset signal (0.2 - 0.5)
    - correlation weights per pair (empirical, recalibrated daily)
    - cross_scale: logistic scale for cross signal (100 - 500)
    - max_own_move: max own-spot return before signal is stale (10 - 50 bps)
    - min_leader_bps: minimum leader move to trigger (20 - 100 bps)

Risk/weakness:
    - Correlations break down during idiosyncratic moves (e.g., SOL-specific news).
    - Requires real-time spot prices for ALL three assets simultaneously.
    - The lag may be shorter than our execution latency, making it untradeable.
    - Requires cross-market state management in the runner (tracking multiple
      assets' spot prices within a single strategy evaluation).

--------------------------------------------------------------------------------
STRATEGY 7: Time Decay Bias Exploiter
--------------------------------------------------------------------------------

Name: TimeDecayBias

Core hypothesis:
    As a 5-minute window progresses without a decisive spot move, the
    probability that the final outcome is YES or NO converges toward the
    current trajectory. Late in the window (>3 min elapsed), small spot
    returns become increasingly predictive because there is less time for
    reversals. If the market midpoint doesn't fully reflect this "time
    decay of uncertainty," there is exploitable edge.

Signal construction:
    time_factor = (elapsed_sec / 300)^gamma, where gamma > 1 to make
    the effect nonlinear (accelerating near expiry).

    p_hat = 0.5 + (spot_return_bps / scale) * time_factor
    Clamp to [0.02, 0.98].

    As time_factor -> 1.0 (near expiry), even small positive spot returns
    push p_hat strongly toward 1.0, and small negative returns push toward 0.0.

    Edge = p_hat - midpoint (for YES) or (1 - p_hat) - (1 - midpoint) (for NO).

    The key insight: when midpoint is near 0.50 late in the window but spot
    has a clear +/- direction, the market is underpricing the time decay.

Entry conditions:
    - elapsed_sec > 150 (second half of window)
    - remaining_sec > 30 (need time for fill)
    - abs(spot_return_bps) > min_spot_move (e.g., 3 bps; any non-trivial trend)
    - edge > min_edge_bps
    - spread_bps < max_spread

Expected edge source:
    Behavioral bias: market participants anchor on the 50% probability and
    are slow to update as time elapses. The "optionality" of reversal shrinks
    super-linearly with time, but human intuition treats it linearly.
    Late-window entries also benefit from lower uncertainty = higher accuracy.

Key tunable parameters:
    - gamma: nonlinearity of time decay (1.5 - 3.0)
    - scale: spot return scaling (50 - 200)
    - min_spot_move: minimum spot return to trade (2 - 15 bps)
    - min_edge_bps: minimum edge (100 - 400)
    - late_entry_start: when to start looking (120 - 210 sec elapsed)

Risk/weakness:
    - Late entries have limited remaining time; execution delays eat into edge.
    - Spot CAN reverse sharply in the last 2 minutes (e.g., large order hits).
    - If the market correctly prices time decay, there is no edge.
    - Higher gamma makes the strategy more aggressive = higher variance.
    - Need to ensure fee doesn't exceed edge (check at near-50% prices).

--------------------------------------------------------------------------------
STRATEGY 8: Liquidity Vacuum Detector
--------------------------------------------------------------------------------

Name: LiquidityVacuum

Core hypothesis:
    When one side of the prediction market orderbook thins out dramatically
    (a "liquidity vacuum"), it signals that market makers are stepping away,
    typically because they expect the price to move in the opposite direction.
    Thin ask depth = MMs expect price to rise (don't want to be short).
    Thin bid depth = MMs expect price to fall (don't want to be long).

Signal construction:
    depth_ratio = bid_depth / ask_depth (> 1 means more bids, thin asks)

    If depth_ratio > vacuum_threshold (e.g., 3.0):
        -> Ask side is thin. MMs are pulling asks. Expect price UP. Buy YES.
    If depth_ratio < 1/vacuum_threshold (e.g., 0.33):
        -> Bid side is thin. MMs are pulling bids. Expect price DOWN. Buy NO.

    Magnitude of conviction:
    log_ratio = ln(depth_ratio)
    p_hat = logistic(log_ratio / sensitivity)

    The log transform makes the signal symmetric and handles extreme ratios.

Entry conditions:
    - abs(log_ratio) > min_log_ratio (e.g., 1.0, meaning 2.7:1 depth ratio)
    - total depth (bid_depth + ask_depth) > min_total_depth (avoid noise on empty books)
    - elapsed_sec in [15, 180]
    - spread_bps < max_spread
    - edge > min_edge_bps

Expected edge source:
    Market makers have superior information about order flow and spot price
    trajectory. When they asymmetrically withdraw liquidity, it leaks their
    private signal. Retail participants on Polymarket don't read depth.

Key tunable parameters:
    - vacuum_threshold: minimum depth ratio to trigger (2.0 - 5.0)
    - sensitivity: logistic scale for log_ratio (0.5 - 2.0)
    - min_total_depth: minimum total depth for signal validity ($50 - $300)
    - min_edge_bps: (100 - 300)
    - require_confirmation: whether to also check spot direction (bool)

Risk/weakness:
    - Depth data can be stale or noisy on low-frequency snapshots.
    - Market makers may have symmetric algos that create temporary vacuums
      without informational content (e.g., during rebalancing).
    - On very thin books, depth_ratio is inherently noisy.
    - Spoofing: placing fake depth on one side to create a false vacuum signal.

--------------------------------------------------------------------------------
STRATEGY 9: Momentum Acceleration
--------------------------------------------------------------------------------

Name: MomentumAcceleration

Core hypothesis:
    It's not just the magnitude of spot return that matters, but its
    acceleration. A spot price that is moving in one direction with
    increasing speed (convex trajectory) is more likely to continue than
    one that is decelerating (concave trajectory). The prediction market
    reacts to the return level but may not fully price the second derivative.

Signal construction:
    Track spot price at regular intervals within the window (e.g., every 5s).

    velocity(t) = spot_return_bps(t) - spot_return_bps(t - dt)  (first derivative)
    acceleration(t) = velocity(t) - velocity(t - dt)  (second derivative)

    More practically, using three sample points (t-2dt, t-dt, t):
    r0 = return at t-2dt, r1 = return at t-dt, r2 = return at t
    velocity = r2 - r1
    acceleration = (r2 - r1) - (r1 - r0) = r2 - 2*r1 + r0

    Composite signal:
    signal_strength = w_return * spot_return_bps + w_accel * acceleration
    p_hat = logistic(signal_strength / scale)

    If acceleration and return have same sign: STRONG signal (momentum building)
    If acceleration opposes return: WEAK signal (momentum fading)

Entry conditions:
    - sign(acceleration) == sign(spot_return_bps) (momentum building, not fading)
    - abs(spot_return_bps) > min_return (e.g., 5 bps)
    - abs(acceleration) > min_acceleration (e.g., 2 bps/interval)
    - elapsed_sec in [30, 180] (need at least 3 data points)
    - edge > min_edge_bps

Expected edge source:
    The prediction market prices the current spot return (level), but does
    not fully incorporate the trajectory shape. Accelerating momentum is
    a stronger predictor of continuation than decelerating momentum of
    the same magnitude.

Key tunable parameters:
    - w_return: weight on spot return level (0.5 - 1.0)
    - w_accel: weight on acceleration (0.3 - 1.0)
    - scale: logistic scale (100 - 400)
    - dt: sampling interval for derivative estimation (5 - 15 seconds)
    - min_acceleration: minimum acceleration to trigger (1 - 5 bps/dt)

Risk/weakness:
    - Numerical differentiation of noisy spot prices amplifies noise.
    - Need smoothing (EMA) on spot prices before taking derivatives.
    - Acceleration can flip rapidly, leading to whipsaw entries.
    - In a random walk, acceleration is pure noise.
    - Requires internal state to track historical spot prices per window.

--------------------------------------------------------------------------------
STRATEGY 10: Contrarian Extreme Fade
--------------------------------------------------------------------------------

Name: ContrarianExtremeFade

Core hypothesis:
    When prediction market midpoint reaches extreme values (> 0.80 or < 0.20)
    early in the window (< 90 seconds elapsed), it often represents an
    overreaction to initial spot movement. The 5-minute window is long enough
    for partial mean reversion. Fading these extremes by buying the opposite
    side at cheap prices (low fee drag!) captures reversion profits.

Signal construction:
    extreme_long = midpoint > extreme_threshold_high (e.g., 0.78)
    extreme_short = midpoint < extreme_threshold_low (e.g., 0.22)

    Mean reversion target: if midpoint > 0.78, estimate "fair" probability
    using a dampened version of the spot signal:
    p_fair = 0.5 + damping * (midpoint - 0.5)
    where damping < 1.0 (e.g., 0.6) reflects expected mean reversion.

    If midpoint > extreme_threshold_high and p_fair < midpoint:
        Buy NO at price (1 - midpoint). Very cheap -> very low fees!
        edge = (1 - p_fair) - (1 - midpoint) = midpoint - p_fair

    If midpoint < extreme_threshold_low and p_fair > midpoint:
        Buy YES at price midpoint. Very cheap -> very low fees!
        edge = p_fair - midpoint

Entry conditions:
    - midpoint > extreme_threshold_high OR midpoint < extreme_threshold_low
    - elapsed_sec < max_elapsed_for_fade (e.g., 90 seconds)
    - remaining_sec > 180 (enough time for mean reversion)
    - edge > min_edge_bps (could be very low since fees are tiny)

Expected edge source:
    1. Fee advantage: at extreme prices, fees are negligible. The round-trip
       (entry + expiry) cost is near zero. This means even tiny edges are
       profitable.
    2. Mean reversion: early extremes in crypto spot often partially reverse.
       A 50bps BTC move in 30 seconds may revert by 60-80% over next 4 minutes.
    3. Behavioral: retail traders pile into momentum at extremes, creating
       temporary overpricing.

Key tunable parameters:
    - extreme_threshold_high: (0.72 - 0.85)
    - extreme_threshold_low: (0.15 - 0.28)
    - damping: mean reversion dampening factor (0.4 - 0.8)
    - max_elapsed_for_fade: max seconds into window (60 - 120)
    - min_remaining_for_fade: min seconds remaining (150 - 240)
    - min_edge_bps: can be very low due to low fees (20 - 100)

Risk/weakness:
    - Sometimes the extreme IS correct (genuine large move sustains).
    - Mean reversion assumption may not hold for crypto (trend-following regime).
    - Buying "cheap" side means you lose almost nothing if wrong, but also
      win small amounts per contract (need high accuracy or large size).
    - Timing is critical: fading too early (< 20s) means the signal hasn't formed;
      fading too late means reversion window is too short.

--------------------------------------------------------------------------------
STRATEGY 11: Spot-Book Consensus
--------------------------------------------------------------------------------

Name: SpotBookConsensus

Core hypothesis:
    The strongest signals occur when MULTIPLE independent indicators agree:
    spot price direction, orderbook imbalance, and prediction market price
    movement. A consensus of these signals is more reliable than any single
    indicator. When they disagree, the correct action is to stay out.

Signal construction:
    Compute three sub-signals, each in [-1, +1]:

    1. Spot signal: S_spot = tanh(spot_return_bps / spot_scale)
       +1 = spot strongly up, -1 = spot strongly down

    2. Book signal: S_book = tanh(OBI / obi_scale)
       where OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)
       +1 = bids dominating, -1 = asks dominating

    3. Price signal: S_price = tanh((midpoint - 0.5) / price_scale)
       +1 = market thinks YES, -1 = market thinks NO

    Consensus score = w1*S_spot + w2*S_book + w3*S_price
    where w1 + w2 + w3 = 1.0

    Agreement metric = min(|S_spot|, |S_book|, |S_price|) * sign_agreement
    where sign_agreement = 1 if all three have same sign, else 0

    p_hat = logistic(consensus_score / consensus_scale)

    Only trade when sign_agreement == 1 (all signals point same direction).

Entry conditions:
    - sign_agreement == 1 (all three sub-signals agree on direction)
    - abs(consensus_score) > min_consensus (e.g., 0.3)
    - min(|S_spot|, |S_book|, |S_price|) > min_weakest_signal (e.g., 0.15)
    - elapsed_sec in [30, 180]
    - edge > min_edge_bps

Expected edge source:
    By requiring consensus, we filter out noise trades where one signal
    fires but others don't confirm. This dramatically reduces false positive
    rate, improving win rate at the cost of fewer trades. Higher win rate
    means fewer losses = better risk-adjusted returns.

Key tunable parameters:
    - w1, w2, w3: sub-signal weights (start with 0.5, 0.3, 0.2)
    - spot_scale: tanh scaling for spot return (20 - 100)
    - obi_scale: tanh scaling for OBI (0.3 - 0.8)
    - price_scale: tanh scaling for midpoint deviation (0.1 - 0.3)
    - consensus_scale: logistic scaling (0.3 - 1.0)
    - min_weakest_signal: minimum magnitude of weakest sub-signal (0.05 - 0.25)

Risk/weakness:
    - Requiring consensus dramatically reduces trade frequency.
    - Some profitable trades are missed when signals temporarily diverge.
    - The three signals may be correlated (not truly independent), reducing
      the filtering benefit.
    - Weight calibration is sensitive and may overfit to historical data.
    - The price signal (midpoint deviation from 0.50) is partially a result
      of the other two signals, creating circularity.

--------------------------------------------------------------------------------
STRATEGY 12: Fee-Optimized Extremes Only
--------------------------------------------------------------------------------

Name: FeeOptimizedExtremes

Core hypothesis:
    The non-linear fee structure creates a massive advantage at extreme prices.
    At midpoint = 0.50, the effective fee is ~1.56% of notional.
    At midpoint = 0.15 or 0.85, the effective fee is ~0.41% / ~0.41%.
    By ONLY trading when the market price is extreme, we drastically reduce
    fee drag, making even moderate directional signals profitable.

Signal construction:
    fee_rate_at_price = 0.25 * (price * (1 - price))^2
    fee_threshold: only trade when fee_rate_at_price < max_fee_rate (e.g., 0.005)
    This corresponds roughly to prices < ~0.20 or > ~0.80.

    At these prices, the market has already moved decisively. Two sub-modes:

    MODE A (Trend Confirmation):
        If midpoint > 0.80 and spot_return_bps confirms direction:
            p_hat = logistic(spot_return_bps / scale)
            If p_hat > midpoint: buy YES. Expensive per contract, but high win rate.
            EV per contract = p_hat - midpoint (with negligible fee)

    MODE B (Contrarian Fade):
        If midpoint > 0.80 and our model estimates p_hat < 0.75:
            Buy NO at cheap price (1 - midpoint) ~ 0.20.
            Win -> collect $1.00 per contract. Lose -> lose $0.20 per contract.
            Payoff ratio is very favorable: 4:1 if buying at 0.20.
            Only need > 20% accuracy to break even (before fees which are ~0).

Entry conditions:
    - midpoint > upper_threshold (e.g., 0.80) OR midpoint < lower_threshold (0.20)
    - edge > min_edge_bps (can be very low, e.g., 50 bps, because fees are small)
    - elapsed_sec > min_elapsed (e.g., 30)
    - spread_bps < max_spread

Expected edge source:
    Pure fee structure exploitation. At extreme prices, the fee formula
    produces fees that are much smaller than at 50%. The extreme price itself
    provides some directional information (the spot has moved significantly),
    which we confirm or fade with our model.

Key tunable parameters:
    - upper_threshold: price above which to trade YES side (0.75 - 0.88)
    - lower_threshold: price below which to trade NO side (0.12 - 0.25)
    - max_fee_rate: maximum acceptable effective fee rate (0.002 - 0.008)
    - min_edge_bps: minimum edge (30 - 150)
    - scale: logistic scale for p_hat estimation (100 - 300)
    - kelly_fraction: can be more aggressive due to low fee drag (0.25 - 0.75)
    - mode: "confirm" (trend following) or "fade" (contrarian) or "adaptive"

Risk/weakness:
    - Low trade frequency: prices only reach extremes in volatile windows.
    - At extreme prices, buying the expensive side (e.g., YES at 0.85) means
      risking 0.85 to gain 0.15. Win rate must be very high.
    - The cheap side (e.g., NO at 0.15) has a favorable payoff (risk 0.15,
      gain 0.85) but low probability of winning. It's a lottery ticket.
    - Strategy degenerates into either "confirm the trend" or "fade the extreme"
      depending on mode; these have opposite risk profiles.


================================================================================
PART III: DETAILED MATHEMATICAL DERIVATIONS
================================================================================

--- 3.1 Logistic Probability Mapping ---

The core mapping from a continuous signal x to a probability p is the logistic:
    p = 1 / (1 + exp(-x / s))

where s is the scale parameter.

Properties:
    - p(x=0) = 0.5 (no signal -> no directional view)
    - p(x -> +inf) -> 1.0
    - p(x -> -inf) -> 0.0
    - dp/dx = p * (1-p) / s (steepest at x=0)

For spot_return_bps as input:
    x = spot_return_bps
    s = scale parameter

    If s = 200: a 20 bps move gives p = 1/(1+exp(-20/200)) = 1/(1+exp(-0.1)) = 0.525
    If s = 50:  a 20 bps move gives p = 1/(1+exp(-20/50)) = 1/(1+exp(-0.4)) = 0.599

    For a 50 bps move:
    s=200: p = 0.562
    s=50:  p = 0.731

    Calibration question: what scale makes the logistic output match empirical
    P(up | spot_return_bps = x, elapsed = t)?

    This requires historical data. The scale should be:
    s_optimal = 1 / beta_1, where beta_1 is the logistic regression coefficient
    from: log(p/(1-p)) = beta_0 + beta_1 * spot_return_bps

--- 3.2 Kelly Criterion for Binary Outcomes ---

Standard Kelly for a binary bet:
    f* = (p * b - q) / b
    where p = win probability, q = 1-p, b = net odds (payout per dollar risked)

In our context:
    Buying YES at price P:
        Win: receive $1.00 per contract. Net profit = 1 - P per contract.
        Lose: receive $0.00. Net loss = P per contract.
        Odds b = (1 - P) / P

    Kelly fraction:
    f* = (p_hat * (1-P)/P - (1-p_hat)) / ((1-P)/P)
       = (p_hat * (1-P) - (1-p_hat) * P) / (1-P)
       = (p_hat - P) / (1 - P)

    This gives the fraction of bankroll to wager.

    Including fees:
    Effective cost per contract = P + fee_per_contract
    Effective profit if win = 1 - P - fee_per_contract
    f* = (p_hat * (1 - P - fee_pc) - (1-p_hat) * (P + fee_pc)) / (1 - P - fee_pc)
       = (p_hat - P - fee_pc) / (1 - P - fee_pc)

    At P=0.50, fee_pc = 0.25 * 0.50 * (0.50*0.50)^2 = 0.25 * 0.50 * 0.0625 = 0.0078
    So effective cost = 0.5078, effective profit if win = 0.4922.
    f* = (p_hat - 0.5078) / 0.4922.  Need p_hat > 0.5078 for f* > 0.

    At P=0.15, fee_pc ~ 0.25 * 0.15 * (0.15*0.85)^2 = 0.25 * 0.15 * 0.0163 = 0.00061
    Effective cost = 0.15061, effective profit = 0.84939.
    f* = (p_hat - 0.15061) / 0.84939.  Need p_hat > 0.151 for f* > 0.

    Fractional Kelly: f_actual = fraction * f*, typically fraction = 0.25.

--- 3.3 Microprice Derivation ---

Standard microprice for a two-sided market:
    microprice = (P_bid * Q_ask + P_ask * Q_bid) / (Q_bid + Q_ask)

where P_bid, P_ask are best bid/ask prices and Q_bid, Q_ask are sizes at the best.

Intuition: if Q_bid >> Q_ask, then:
    microprice -> P_ask (fair value is near the ask; aggressive buying)

If Q_ask >> Q_bid:
    microprice -> P_bid (fair value is near the bid; aggressive selling)

For Polymarket binary markets where price = probability:
    microprice directly estimates the "true" probability as implied by
    the relative aggression of buyers vs sellers.

Extension: volume-weighted microprice using top-N levels:
    microprice_ext = sum(P_bid_i * Q_ask_i + P_ask_i * Q_bid_i)
                     / sum(Q_bid_i + Q_ask_i)  for i in 1..N

    This uses more of the book but is noisier on thin books.

--- 3.4 Time Decay Model ---

Model the 5-minute binary outcome as a continuous-time process.
Let X(t) = spot return from start of window to time t (in bps).
Window ends at T = 300 seconds.

If X(t) follows a random walk with drift mu and volatility sigma:
    X(T) | X(t) ~ N(X(t) + mu*(T-t), sigma^2 * (T-t))

P(X(T) > 0 | X(t)) = Phi((X(t) + mu*(T-t)) / (sigma * sqrt(T-t)))

where Phi is the standard normal CDF.

This gives us a CLOSED-FORM probability estimate!

At t=0: P = Phi(mu*T / (sigma*sqrt(T))) = Phi(mu*sqrt(T)/sigma)
At t close to T: if X(t) > 0, then sigma*sqrt(T-t) -> 0, so P -> 1.
If X(t) < 0 and t -> T, P -> 0.

Practical implementation:
    p_hat(t) = Phi(X(t) / (sigma_est * sqrt(remaining_sec)))

    where sigma_est is estimated from intra-window tick data (see Strategy 4).

    This is more principled than the logistic + gamma approach in Strategy 7,
    and should be the preferred model if we have a volatility estimate.

    If we don't have sigma_est, use the logistic approximation:
    p_hat ~ logistic(X(t) / s * (elapsed_sec / 300)^gamma)

--- 3.5 Order Book Imbalance Predictive Power ---

Academic literature (Cao, Chen, Griffin 2005; Chordia, Roll, Subrahmanyam 2002)
shows that order book imbalance predicts short-term returns with an R^2 of
2-5% on traditional equity markets.

In prediction markets, the relationship is potentially stronger because:
1. Less algorithmic competition (fewer quants reading the book).
2. Direct mapping: price = probability, so book imbalance directly predicts
   the binary outcome.
3. Fewer levels of the book, so top-of-book imbalance captures more info.

Potential R^2 for 5-minute binary outcome prediction: 3-8% (speculative).
This translates to an edge of approximately 50-150 bps, which is marginal
at p=0.50 (need 156 bps for fees) but highly profitable at p=0.20 or 0.80
(need <65 bps for fees).

--- 3.6 Cross-Asset Correlation Mathematics ---

Let R_BTC(t), R_ETH(t), R_SOL(t) be spot returns at time t within a window.

Empirical 5-minute correlation matrix (typical values from crypto data):
    |        | BTC   | ETH   | SOL   |
    |--------|-------|-------|-------|
    | BTC    | 1.00  | 0.85  | 0.75  |
    | ETH    | 0.85  | 1.00  | 0.80  |
    | SOL    | 0.75  | 0.80  | 1.00  |

Lead-lag: BTC returns at time t predict ETH returns at t+delta, where
delta ~ 1-5 seconds. This creates a tradeable window.

The optimal predictor for ETH return given BTC return:
    E[R_ETH | R_BTC = r] = rho_BTC_ETH * (sigma_ETH / sigma_BTC) * r
                         = 0.85 * (sigma_ETH / sigma_BTC) * r

If sigma_ETH ~ 1.2 * sigma_BTC (ETH is typically more volatile):
    E[R_ETH | R_BTC = r] = 0.85 * 1.2 * r = 1.02 * r

So a 50 bps BTC move predicts a ~51 bps ETH move. If the ETH prediction
market hasn't moved yet, this is a ~51 bps edge (before fees). At extreme
prices, this is more than enough.


================================================================================
PART IV: MARKETSTATE EXTENSION SPECIFICATION
================================================================================

The current MarketState has these fields:
    condition_id, yes_token_id, asset, slug,
    best_bid, best_ask, spread, spread_bps, midpoint, bid_depth, ask_depth,
    spot_price, spot_price_at_window_start, spot_return_bps,
    window_start_ts, window_end_ts, elapsed_sec, remaining_sec, ts

Required extensions for proposed strategies:

--- Extension 1: Raw Book Levels ---
Needed by: Strategy 5 (Microprice), indirectly useful for 1, 8
Currently available in ClobSnapshot but not passed to MarketState.

    @dataclass
    class MarketState:
        ...
        # NEW: raw order book levels (from ClobSnapshot)
        bids: List[Tuple[float, float]] = field(default_factory=list)
        asks: List[Tuple[float, float]] = field(default_factory=list)
        bid_size_at_best: float = 0.0    # size at best bid
        ask_size_at_best: float = 0.0    # size at best ask

Runner change in _build_state():
    bid_size_at_best = float(snap.bids[0][1]) if snap.bids else 0.0
    ask_size_at_best = float(snap.asks[0][1]) if snap.asks else 0.0

--- Extension 2: Cross-Asset Spot Prices ---
Needed by: Strategy 6 (CrossAssetCorrelation)

    @dataclass
    class MarketState:
        ...
        # NEW: cross-asset data
        other_spot_returns: Dict[str, float] = field(default_factory=dict)
        # e.g., {"BTC": 25.3, "SOL": -12.1} when evaluating an ETH market

Runner change in _build_state():
    other_returns = {}
    for sym_key, spot_val in self._spot_prices.items():
        asset_name = sym_key.replace("usdt", "").upper()
        if asset_name != market.asset:
            start_key = ???  # need to track window-start prices per asset
            # This is complex: we need window-start spot for OTHER assets too.
            # Simplest: track a "global spot at window start" dict.
            pass

    Alternative: give the strategy a reference to a shared spot price store
    that it can query directly. This avoids polluting MarketState.

--- Extension 3: Microprice (Computed Field) ---
Needed by: Strategy 5 (Microprice)
Can be computed from Extension 1 data.

    @dataclass
    class MarketState:
        ...
        # NEW: derived microstructure fields
        microprice: float = 0.0

Runner change:
    if snap.bids and snap.asks:
        bid_sz = float(snap.bids[0][1])
        ask_sz = float(snap.asks[0][1])
        if bid_sz + ask_sz > 0:
            microprice = (snap.best_bid * ask_sz + snap.best_ask * bid_sz) / (bid_sz + ask_sz)
        else:
            microprice = midpoint

IMPLEMENTATION RECOMMENDATION:
    Start with Extension 1 only. It enables Microprice computation within
    the strategy itself (no need for the runner to compute it). Extension 2
    is the most invasive change and should only be done when implementing
    Strategy 6. Extension 3 is a convenience that can be deferred.


================================================================================
PART V: CONCRETE IMPLEMENTATION SKELETONS (TIER 1 STRATEGIES)
================================================================================

--- 5.1 FeeOptimizedExtremes (Strategy 12) ---
Priority: HIGHEST. Exploits structural fee advantage. Simple to implement.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from strategies.base import BaseStrategy, MarketState, Signal


def _logistic(x: float) -> float:
    """Logistic sigmoid: maps any real number to (0, 1)."""
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _effective_fee_rate(price: float) -> float:
    """Effective fee rate at a given price level."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


def _kelly_fraction_with_fees(p_hat: float, entry_price: float,
                               fee_rate: float, fraction: float = 0.25) -> float:
    """
    Kelly fraction accounting for the actual fee structure.
    For hold-to-expiry binary bets.
    """
    fee_per_contract = entry_price * fee_rate
    effective_cost = entry_price + fee_per_contract
    effective_profit = 1.0 - effective_cost  # profit per contract if we win

    if effective_profit <= 0 or p_hat <= 0 or p_hat >= 1:
        return 0.0

    # Kelly for binary: f* = (p * profit - q * cost) / profit
    # where profit = 1 - effective_cost, cost = effective_cost
    ev = p_hat * effective_profit - (1 - p_hat) * effective_cost
    if ev <= 0:
        return 0.0

    f_star = ev / effective_profit
    return max(0.0, min(1.0, f_star * fraction))


# ---------------------------------------------------------------------------
# STRATEGY 12 IMPLEMENTATION: FeeOptimizedExtremes
# ---------------------------------------------------------------------------

@dataclass
class FeeOptimizedExtremesConfig:
    """Configuration for FeeOptimizedExtremes strategy."""
    # Price thresholds defining "extreme" territory
    upper_threshold: float = 0.78       # midpoint above this -> consider trading
    lower_threshold: float = 0.22       # midpoint below this -> consider trading

    # Fee constraint
    max_effective_fee_rate: float = 0.008  # only trade when fee rate < this

    # Signal parameters
    logistic_scale: float = 150.0       # scale for spot return -> p_hat mapping
    min_edge_bps: float = 50.0          # minimum edge to trade (low! because fees are tiny)

    # Sizing
    kelly_fraction: float = 0.35        # more aggressive than standard (fees low)
    bankroll: float = 10_000.0
    max_size: int = 300

    # Timing
    min_elapsed_sec: int = 20
    max_elapsed_sec: int = 240

    # Market quality
    max_spread_bps: float = 400.0
    slippage_tolerance_bps: int = 250

    # Mode: "confirm" buys the side the market leans toward
    #        "fade" buys the opposite (contrarian)
    #        "adaptive" confirms if spot agrees, fades if spot disagrees
    mode: str = "adaptive"


class FeeOptimizedExtremesStrategy(BaseStrategy):
    """
    Only trades when market prices are at extremes (< lower_threshold or
    > upper_threshold) where the non-linear fee formula produces minimal fees.

    In "adaptive" mode:
    - If spot return agrees with market extreme -> CONFIRM (ride momentum)
    - If spot return disagrees with market extreme -> FADE (mean reversion)
    """

    name = "fee_optimized_extremes"

    def __init__(self, config: FeeOptimizedExtremesConfig = None):
        self.config = config or FeeOptimizedExtremesConfig()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # Timing filter
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late in window")

        # Spread filter
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f} bps too wide")

        # Fee filter: is the market at an extreme enough price?
        mid = state.midpoint
        fee_rate = _effective_fee_rate(mid)
        if fee_rate > cfg.max_effective_fee_rate:
            return Signal("hold", 0, 0,
                          f"fee rate {fee_rate:.4f} > max {cfg.max_effective_fee_rate:.4f}")

        # Determine which extreme we are at
        is_high = mid > cfg.upper_threshold
        is_low = mid < cfg.lower_threshold

        if not is_high and not is_low:
            return Signal("hold", 0, 0, "not at extreme price")

        # Compute our probability estimate from spot
        p_hat = _logistic(state.spot_return_bps / 10_000 / (cfg.logistic_scale / 10_000))
        # Simplify: p_hat = logistic(spot_return_bps / logistic_scale)
        p_hat = _logistic(state.spot_return_bps / cfg.logistic_scale)

        # Determine action based on mode
        if cfg.mode == "confirm":
            action, entry_price, p_for_kelly = self._confirm_mode(
                is_high, is_low, p_hat, mid)
        elif cfg.mode == "fade":
            action, entry_price, p_for_kelly = self._fade_mode(
                is_high, is_low, p_hat, mid)
        else:  # adaptive
            action, entry_price, p_for_kelly = self._adaptive_mode(
                is_high, is_low, p_hat, mid, state.spot_return_bps)

        if action == "hold":
            return Signal("hold", 0, 0, "no edge at extreme", p_hat=p_hat)

        # Edge check
        ev_bps = abs(p_for_kelly - entry_price) * 10_000
        if ev_bps < cfg.min_edge_bps:
            return Signal("hold", 0, 0, f"edge {ev_bps:.0f} bps < min {cfg.min_edge_bps:.0f}",
                          p_hat=p_hat, ev_bps=ev_bps)

        # Kelly sizing with fee-adjusted calculation
        actual_fee_rate = _effective_fee_rate(entry_price)
        f = _kelly_fraction_with_fees(p_for_kelly, entry_price,
                                       actual_fee_rate, cfg.kelly_fraction)
        size = int(cfg.bankroll * f / entry_price) if entry_price > 0 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly size zero", p_hat=p_hat, ev_bps=ev_bps)

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=(f"extreme_{cfg.mode}: mid={mid:.3f}, fee={actual_fee_rate:.5f}, "
                       f"spot={state.spot_return_bps:.0f}bps, edge={ev_bps:.0f}bps"),
            p_hat=p_for_kelly,
            ev_bps=ev_bps,
        )

    def _confirm_mode(self, is_high, is_low, p_hat, mid):
        """Confirm the extreme: buy the side the market leans toward."""
        if is_high:
            # Market says YES likely. Buy YES if our model agrees.
            if p_hat > mid:
                return "buy_yes", mid, p_hat
        elif is_low:
            # Market says NO likely. Buy NO if our model agrees.
            p_no_hat = 1.0 - p_hat
            p_no_market = 1.0 - mid
            if p_no_hat > p_no_market:
                return "buy_no", p_no_market, p_no_hat
        return "hold", 0, 0

    def _fade_mode(self, is_high, is_low, p_hat, mid):
        """Fade the extreme: buy the opposite side (contrarian)."""
        if is_high:
            # Market says YES at high probability. Buy cheap NO.
            p_no_hat = 1.0 - p_hat
            no_price = 1.0 - mid
            if p_no_hat > no_price:  # we think NO is underpriced
                return "buy_no", no_price, p_no_hat
        elif is_low:
            # Market says NO likely. Buy cheap YES.
            if p_hat > mid:  # we think YES is underpriced
                return "buy_yes", mid, p_hat
        return "hold", 0, 0

    def _adaptive_mode(self, is_high, is_low, p_hat, mid, spot_return_bps):
        """
        Confirm if spot agrees with market extreme, fade if spot disagrees.
        Spot moving UP + market HIGH = spot confirms -> confirm.
        Spot moving DOWN + market HIGH = spot disagrees -> fade.
        """
        if is_high:
            if spot_return_bps > 0:
                # Spot confirms upward move -> confirm (buy YES)
                return self._confirm_mode(is_high, is_low, p_hat, mid)
            else:
                # Spot disagrees with high market -> fade (buy NO)
                return self._fade_mode(is_high, is_low, p_hat, mid)
        elif is_low:
            if spot_return_bps < 0:
                # Spot confirms downward move -> confirm (buy NO)
                return self._confirm_mode(is_high, is_low, p_hat, mid)
            else:
                # Spot disagrees with low market -> fade (buy YES)
                return self._fade_mode(is_high, is_low, p_hat, mid)
        return "hold", 0, 0

    def reset(self):
        pass


# ---------------------------------------------------------------------------
# STRATEGY 7 IMPLEMENTATION: TimeDecayBias
# ---------------------------------------------------------------------------

@dataclass
class TimeDecayBiasConfig:
    """Configuration for TimeDecayBias strategy."""
    gamma: float = 2.0                  # nonlinearity exponent for time factor
    scale: float = 100.0                # logistic scale for spot return
    min_spot_move_bps: float = 3.0      # minimum spot return to act on
    min_edge_bps: float = 80.0          # minimum edge (lower than standard: late entry has less fee drag)
    late_entry_start_sec: int = 150     # start looking after this many seconds
    min_remaining_sec: int = 30         # need at least this long for execution
    max_spread_bps: float = 400.0
    kelly_fraction: float = 0.30
    bankroll: float = 10_000.0
    max_size: int = 250
    slippage_tolerance_bps: int = 200


class TimeDecayBiasStrategy(BaseStrategy):
    """
    Exploits the nonlinear increase in spot return predictiveness as the
    5-minute window progresses. Late in the window, even small spot returns
    become strong predictors of the final outcome.

    Uses time_factor = (elapsed/300)^gamma to amplify the spot signal
    nonlinearly with elapsed time.
    """

    name = "time_decay_bias"

    def __init__(self, config: TimeDecayBiasConfig = None):
        self.config = config or TimeDecayBiasConfig()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # Only trade in the second half of the window
        if state.elapsed_sec < cfg.late_entry_start_sec:
            return Signal("hold", 0, 0, "too early for time decay strategy")

        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, f"only {state.remaining_sec}s remaining")

        # Spread filter
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f} bps too wide")

        # Need a non-trivial spot move
        if abs(state.spot_return_bps) < cfg.min_spot_move_bps:
            return Signal("hold", 0, 0,
                          f"spot move {state.spot_return_bps:.1f} bps too small")

        # Time-weighted probability estimate
        time_factor = (state.elapsed_sec / 300.0) ** cfg.gamma
        # Scale the spot return by time factor and map through logistic
        adjusted_signal = (state.spot_return_bps / cfg.scale) * time_factor
        p_hat = _logistic(adjusted_signal)
        # Clamp to avoid extreme probabilities
        p_hat = max(0.02, min(0.98, p_hat))

        # Market price
        p_market = state.midpoint

        # Edge
        ev_bps = (p_hat - p_market) * 10_000

        if abs(ev_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"no edge: ev={ev_bps:.0f} bps, time_factor={time_factor:.3f}",
                          p_hat=p_hat, ev_bps=ev_bps)

        # Direction
        if ev_bps > 0:
            action = "buy_yes"
            entry_price = p_market
            p_for_kelly = p_hat
        else:
            action = "buy_no"
            p_hat = 1.0 - p_hat
            entry_price = 1.0 - p_market
            p_for_kelly = p_hat
            ev_bps = abs(ev_bps)

        # Fee-aware Kelly sizing
        fee_rate = _effective_fee_rate(entry_price)
        f = _kelly_fraction_with_fees(p_for_kelly, entry_price, fee_rate, cfg.kelly_fraction)
        size = int(cfg.bankroll * f / entry_price) if entry_price > 0 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly size zero", p_hat=p_for_kelly, ev_bps=ev_bps)

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=(f"time_decay: elapsed={state.elapsed_sec}s, "
                       f"tf={time_factor:.3f}, spot={state.spot_return_bps:.0f}bps, "
                       f"edge={ev_bps:.0f}bps, fee={fee_rate:.5f}"),
            p_hat=p_for_kelly,
            ev_bps=ev_bps,
        )

    def reset(self):
        pass


# ---------------------------------------------------------------------------
# STRATEGY 3 IMPLEMENTATION: SpotPredictionDivergence
# ---------------------------------------------------------------------------

@dataclass
class SpotPredictionDivergenceConfig:
    """Configuration for SpotPredictionDivergence strategy."""
    scale_factor: float = 200.0         # logistic scale for spot return
    min_divergence: float = 0.04        # minimum p_spot - midpoint divergence
    time_weight_ramp_sec: float = 120.0 # time weight reaches 1.0 at this elapsed
    min_spot_move_bps: float = 5.0      # minimum spot return for signal
    min_edge_bps: float = 150.0         # minimum edge in bps
    min_elapsed_sec: int = 45
    max_elapsed_sec: int = 200
    max_spread_bps: float = 400.0
    kelly_fraction: float = 0.25
    bankroll: float = 10_000.0
    max_size: int = 200
    slippage_tolerance_bps: int = 200


class SpotPredictionDivergenceStrategy(BaseStrategy):
    """
    Trades the divergence between spot-implied probability and prediction
    market midpoint. When spot strongly suggests up but the market hasn't
    fully repriced, buy YES (and vice versa).

    Time-weights the signal: later in the window, the spot return is more
    "locked in" and the divergence is more meaningful.
    """

    name = "spot_prediction_divergence"

    def __init__(self, config: SpotPredictionDivergenceConfig = None):
        self.config = config or SpotPredictionDivergenceConfig()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # Timing filter
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")

        # Spread filter
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f} bps too wide")

        # Need meaningful spot move
        if abs(state.spot_return_bps) < cfg.min_spot_move_bps:
            return Signal("hold", 0, 0, "spot move too small")

        # Spot-implied probability
        p_spot = _logistic(state.spot_return_bps / cfg.scale_factor)

        # Raw divergence
        divergence = p_spot - state.midpoint

        # Time-weight: stronger signal later in the window
        time_weight = min(state.elapsed_sec / cfg.time_weight_ramp_sec, 1.0)
        adjusted_div = divergence * time_weight

        if abs(adjusted_div) < cfg.min_divergence:
            return Signal("hold", 0, 0,
                          f"divergence {adjusted_div:.4f} < min {cfg.min_divergence:.4f}",
                          p_hat=p_spot)

        # Use the time-weighted estimate as our p_hat
        # Blend: p_hat = midpoint + adjusted_divergence
        p_hat = state.midpoint + adjusted_div
        p_hat = max(0.02, min(0.98, p_hat))

        # Edge
        ev_bps = (p_hat - state.midpoint) * 10_000

        if abs(ev_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0, f"edge {ev_bps:.0f} bps too small",
                          p_hat=p_hat, ev_bps=ev_bps)

        # Direction
        if ev_bps > 0:
            action = "buy_yes"
            entry_price = state.midpoint
            p_for_kelly = p_hat
        else:
            action = "buy_no"
            p_hat = 1.0 - p_hat
            entry_price = 1.0 - state.midpoint
            p_for_kelly = p_hat
            ev_bps = abs(ev_bps)

        # Sizing
        fee_rate = _effective_fee_rate(entry_price)
        f = _kelly_fraction_with_fees(p_for_kelly, entry_price, fee_rate, cfg.kelly_fraction)
        size = int(cfg.bankroll * f / entry_price) if entry_price > 0 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly size zero", p_hat=p_for_kelly, ev_bps=ev_bps)

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=(f"divergence: p_spot={p_spot:.3f}, mid={state.midpoint:.3f}, "
                       f"tw={time_weight:.2f}, div={adjusted_div:.4f}, edge={ev_bps:.0f}bps"),
            p_hat=p_for_kelly,
            ev_bps=ev_bps,
        )

    def reset(self):
        pass


# ---------------------------------------------------------------------------
# STRATEGY 10 IMPLEMENTATION: ContrarianExtremeFade
# ---------------------------------------------------------------------------

@dataclass
class ContrarianExtremeFadeConfig:
    """Configuration for ContrarianExtremeFade strategy."""
    extreme_threshold_high: float = 0.78
    extreme_threshold_low: float = 0.22
    damping: float = 0.6               # mean reversion factor (< 1.0)
    max_elapsed_for_fade: int = 90      # only fade early in the window
    min_remaining_for_fade: int = 180   # need enough time for reversion
    min_edge_bps: float = 30.0          # very low threshold (fees are tiny!)
    spot_confirmation_weight: float = 0.3  # how much to weight spot signal
    kelly_fraction: float = 0.40        # aggressive (cheap contracts)
    bankroll: float = 10_000.0
    max_size: int = 500                 # can be large (cheap contracts)
    max_spread_bps: float = 500.0
    slippage_tolerance_bps: int = 300


class ContrarianExtremeFadeStrategy(BaseStrategy):
    """
    Fades extreme prediction market prices early in the window. When midpoint
    reaches > 0.78 or < 0.22 within the first 90 seconds, buys the cheap
    side, betting on mean reversion.

    Key advantage: the cheap side has negligible fees AND favorable payoff
    asymmetry (risk small, win large). Only needs modest win rate.
    """

    name = "contrarian_extreme_fade"

    def __init__(self, config: ContrarianExtremeFadeConfig = None):
        self.config = config or ContrarianExtremeFadeConfig()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # Only fade early in the window
        if state.elapsed_sec > cfg.max_elapsed_for_fade:
            return Signal("hold", 0, 0, "too late to fade")

        if state.remaining_sec < cfg.min_remaining_for_fade:
            return Signal("hold", 0, 0, "not enough time for reversion")

        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f} bps too wide")

        mid = state.midpoint

        # Check for extreme prices
        if mid <= cfg.extreme_threshold_low or mid >= cfg.extreme_threshold_high:
            pass  # continue
        else:
            return Signal("hold", 0, 0, "not at extreme price")

        # Compute dampened fair value (mean reversion estimate)
        p_fair = 0.5 + cfg.damping * (mid - 0.5)

        # Optionally blend with spot-implied probability
        if abs(state.spot_return_bps) > 1.0:
            p_spot = _logistic(state.spot_return_bps / 150.0)
            p_fair = (1.0 - cfg.spot_confirmation_weight) * p_fair + \
                     cfg.spot_confirmation_weight * p_spot

        p_fair = max(0.02, min(0.98, p_fair))

        if mid >= cfg.extreme_threshold_high:
            # Market says strong YES. We fade -> buy NO (cheap side).
            # NO price = 1 - mid. We think fair NO probability = 1 - p_fair.
            p_no_hat = 1.0 - p_fair
            no_price = 1.0 - mid  # this is the entry price for NO
            edge_bps = (p_no_hat - no_price) * 10_000

            if edge_bps < cfg.min_edge_bps:
                return Signal("hold", 0, 0,
                              f"fade edge {edge_bps:.0f} bps too small",
                              p_hat=p_fair, ev_bps=-edge_bps)

            # Size (NO contracts are cheap!)
            fee_rate = _effective_fee_rate(no_price)
            f = _kelly_fraction_with_fees(p_no_hat, no_price, fee_rate, cfg.kelly_fraction)
            size = int(cfg.bankroll * f / no_price) if no_price > 0 else 0
            size = min(size, cfg.max_size)
            size = max(size, 1) if f > 0 else 0

            if size == 0:
                return Signal("hold", 0, 0, "kelly size zero")

            return Signal(
                action="buy_no",
                size=size,
                max_slippage_bps=cfg.slippage_tolerance_bps,
                rationale=(f"fade_high: mid={mid:.3f}, p_fair={p_fair:.3f}, "
                           f"no_price={no_price:.3f}, edge={edge_bps:.0f}bps, "
                           f"fee={fee_rate:.6f}"),
                p_hat=p_no_hat,
                ev_bps=edge_bps,
            )

        elif mid <= cfg.extreme_threshold_low:
            # Market says strong NO. We fade -> buy YES (cheap side).
            p_yes_hat = p_fair
            yes_price = mid  # this is the entry price for YES
            edge_bps = (p_yes_hat - yes_price) * 10_000

            if edge_bps < cfg.min_edge_bps:
                return Signal("hold", 0, 0,
                              f"fade edge {edge_bps:.0f} bps too small",
                              p_hat=p_fair, ev_bps=edge_bps)

            fee_rate = _effective_fee_rate(yes_price)
            f = _kelly_fraction_with_fees(p_yes_hat, yes_price, fee_rate, cfg.kelly_fraction)
            size = int(cfg.bankroll * f / yes_price) if yes_price > 0 else 0
            size = min(size, cfg.max_size)
            size = max(size, 1) if f > 0 else 0

            if size == 0:
                return Signal("hold", 0, 0, "kelly size zero")

            return Signal(
                action="buy_yes",
                size=size,
                max_slippage_bps=cfg.slippage_tolerance_bps,
                rationale=(f"fade_low: mid={mid:.3f}, p_fair={p_fair:.3f}, "
                           f"yes_price={yes_price:.3f}, edge={edge_bps:.0f}bps, "
                           f"fee={fee_rate:.6f}"),
                p_hat=p_yes_hat,
                ev_bps=edge_bps,
            )

        return Signal("hold", 0, 0, "no extreme detected")

    def reset(self):
        pass


# ---------------------------------------------------------------------------
# STRATEGY 4 IMPLEMENTATION: VolatilityRegime
# ---------------------------------------------------------------------------

@dataclass
class VolatilityRegimeConfig:
    """Configuration for VolatilityRegime strategy."""
    vol_low_threshold: float = 8.0      # below this stdev(bps), don't trade
    vol_high_threshold: float = 35.0    # above this, boost sizing
    vol_baseline: float = 20.0          # normalization constant
    logistic_scale: float = 0.02        # same as SpotMomentum default
    min_edge_bps: float = 200.0
    high_vol_size_multiplier: float = 1.5
    min_ticks_for_vol: int = 8          # minimum observations before valid
    min_elapsed_sec: int = 60           # need ticks to estimate vol
    max_elapsed_sec: int = 200
    max_spread_bps: float = 400.0
    kelly_fraction: float = 0.25
    bankroll: float = 10_000.0
    max_size: int = 250
    slippage_tolerance_bps: int = 200


class VolatilityRegimeStrategy(BaseStrategy):
    """
    Wraps the spot momentum signal with a volatility regime filter.
    Does NOT trade in low-vol windows (where fees erase any edge).
    Boosts sizing in high-vol windows (where prices reach extremes
    and fees are minimal).

    Maintains internal state to track spot returns across ticks within
    each window.
    """

    name = "volatility_regime"

    def __init__(self, config: VolatilityRegimeConfig = None):
        self.config = config or VolatilityRegimeConfig()
        self._spot_history: Dict[str, List[float]] = {}

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config
        cid = state.condition_id

        # Accumulate spot return history for this window
        if cid not in self._spot_history:
            self._spot_history[cid] = []
        self._spot_history[cid].append(state.spot_return_bps)

        # Timing filter
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early (building vol estimate)")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")

        # Spread filter
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread too wide")

        # Compute realized volatility
        history = self._spot_history[cid]
        if len(history) < cfg.min_ticks_for_vol:
            return Signal("hold", 0, 0,
                          f"need {cfg.min_ticks_for_vol} ticks, have {len(history)}")

        # Standard deviation of spot returns (change between consecutive readings)
        changes = [history[i] - history[i-1] for i in range(1, len(history))]
        if len(changes) < 2:
            return Signal("hold", 0, 0, "not enough changes for vol")

        mean_change = sum(changes) / len(changes)
        var = sum((c - mean_change) ** 2 for c in changes) / (len(changes) - 1)
        vol_realized = math.sqrt(var) if var > 0 else 0.0

        # Regime classification
        if vol_realized < cfg.vol_low_threshold:
            return Signal("hold", 0, 0,
                          f"low vol regime: {vol_realized:.1f} bps < {cfg.vol_low_threshold:.1f}")

        # Volatility-adjusted logistic
        vol_adj = max(vol_realized / cfg.vol_baseline, 0.5)
        p_hat = _logistic(state.spot_return_bps / 10_000 / (cfg.logistic_scale * vol_adj))

        p_market = state.midpoint
        ev_bps = (p_hat - p_market) * 10_000

        if abs(ev_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"no edge: ev={ev_bps:.0f}bps, vol={vol_realized:.1f}",
                          p_hat=p_hat, ev_bps=ev_bps)

        # Direction
        if ev_bps > 0:
            action = "buy_yes"
            entry_price = p_market
            p_for_kelly = p_hat
        else:
            action = "buy_no"
            p_hat = 1.0 - p_hat
            p_market_no = 1.0 - p_market
            entry_price = p_market_no
            p_for_kelly = p_hat
            ev_bps = abs(ev_bps)

        # Sizing with regime-based multiplier
        fee_rate = _effective_fee_rate(entry_price)
        f = _kelly_fraction_with_fees(p_for_kelly, entry_price, fee_rate, cfg.kelly_fraction)
        size = int(cfg.bankroll * f / entry_price) if entry_price > 0 else 0

        # Boost in high-vol regime
        if vol_realized > cfg.vol_high_threshold:
            size = int(size * cfg.high_vol_size_multiplier)

        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly size zero", p_hat=p_for_kelly, ev_bps=ev_bps)

        regime = "HIGH" if vol_realized > cfg.vol_high_threshold else "MEDIUM"
        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=(f"vol_regime={regime}, vol={vol_realized:.1f}bps, "
                       f"spot={state.spot_return_bps:.0f}bps, edge={ev_bps:.0f}bps"),
            p_hat=p_for_kelly,
            ev_bps=ev_bps,
        )

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._spot_history.pop(condition_id, None)

    def reset(self):
        self._spot_history.clear()


# ---------------------------------------------------------------------------
# STRATEGY 1 IMPLEMENTATION: OrderBookImbalance
# ---------------------------------------------------------------------------

@dataclass
class OrderBookImbalanceConfig:
    """Configuration for OrderBookImbalance strategy."""
    obi_threshold: float = 0.25        # minimum OBI magnitude to trigger
    beta: float = 0.10                 # OBI -> probability sensitivity
    alpha: float = 0.3                 # EMA smoothing factor
    min_edge_bps: float = 150.0
    min_total_depth: float = 50.0      # minimum bid+ask depth in dollars
    min_elapsed_sec: int = 30
    max_elapsed_sec: int = 180
    max_spread_bps: float = 400.0
    kelly_fraction: float = 0.25
    bankroll: float = 10_000.0
    max_size: int = 200
    slippage_tolerance_bps: int = 200


class OrderBookImbalanceStrategy(BaseStrategy):
    """
    Trades based on persistent orderbook imbalance on the prediction market.
    Uses EMA-smoothed OBI (order book imbalance) as a probability adjustment
    on top of the midpoint.
    """

    name = "order_book_imbalance"

    def __init__(self, config: OrderBookImbalanceConfig = None):
        self.config = config or OrderBookImbalanceConfig()
        self._obi_ema: Dict[str, float] = {}
        self._tick_count: Dict[str, int] = {}

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config
        cid = state.condition_id

        # Compute current OBI
        total_depth = state.bid_depth + state.ask_depth
        if total_depth < cfg.min_total_depth:
            return Signal("hold", 0, 0,
                          f"total depth ${total_depth:.0f} < min ${cfg.min_total_depth:.0f}")

        obi = (state.bid_depth - state.ask_depth) / total_depth

        # EMA smoothing
        if cid not in self._obi_ema:
            self._obi_ema[cid] = obi
            self._tick_count[cid] = 1
        else:
            self._obi_ema[cid] = cfg.alpha * obi + (1.0 - cfg.alpha) * self._obi_ema[cid]
            self._tick_count[cid] += 1

        obi_ema = self._obi_ema[cid]

        # Timing filter
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")

        # Spread filter
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, "spread too wide")

        # Need significant imbalance
        if abs(obi_ema) < cfg.obi_threshold:
            return Signal("hold", 0, 0,
                          f"OBI_ema {obi_ema:.3f} < threshold {cfg.obi_threshold:.3f}")

        # Probability estimate
        p_hat = state.midpoint + cfg.beta * obi_ema
        p_hat = max(0.02, min(0.98, p_hat))

        p_market = state.midpoint
        ev_bps = (p_hat - p_market) * 10_000

        if abs(ev_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0, f"edge {ev_bps:.0f} bps too small",
                          p_hat=p_hat, ev_bps=ev_bps)

        # Direction
        if ev_bps > 0:
            action = "buy_yes"
            entry_price = p_market
            p_for_kelly = p_hat
        else:
            action = "buy_no"
            p_hat = 1.0 - p_hat
            entry_price = 1.0 - p_market
            p_for_kelly = p_hat
            ev_bps = abs(ev_bps)

        # Sizing
        fee_rate = _effective_fee_rate(entry_price)
        f = _kelly_fraction_with_fees(p_for_kelly, entry_price, fee_rate, cfg.kelly_fraction)
        size = int(cfg.bankroll * f / entry_price) if entry_price > 0 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly size zero", p_hat=p_for_kelly, ev_bps=ev_bps)

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=(f"obi: OBI_ema={obi_ema:.3f}, ticks={self._tick_count[cid]}, "
                       f"edge={ev_bps:.0f}bps"),
            p_hat=p_for_kelly,
            ev_bps=ev_bps,
        )

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._obi_ema.pop(condition_id, None)
        self._tick_count.pop(condition_id, None)

    def reset(self):
        self._obi_ema.clear()
        self._tick_count.clear()


"""
================================================================================
PART VI: PARAMETER SWEEP PLANS & BACKTESTING METHODOLOGY
================================================================================

--- 6.1 Strategy-Specific Sweep Grids ---

The sweep infrastructure (cli/sweep.py) currently sweeps RiskConfig parameters.
To sweep strategy-specific parameters, we need to extend it to also sweep
strategy config objects.

Proposed extension to cli/sweep.py:

    STRATEGY_SWEEP_GRIDS = {
        "fee_optimized_extremes": {
            "upper_threshold": [0.75, 0.78, 0.82, 0.85],
            "lower_threshold": [0.15, 0.18, 0.22, 0.25],
            "logistic_scale": [100, 150, 200, 300],
            "min_edge_bps": [30, 50, 80, 120],
            "mode": ["confirm", "fade", "adaptive"],
        },
        "time_decay_bias": {
            "gamma": [1.5, 2.0, 2.5, 3.0],
            "scale": [50, 100, 150, 200],
            "late_entry_start_sec": [120, 150, 180, 210],
            "min_edge_bps": [50, 80, 120, 200],
        },
        "spot_prediction_divergence": {
            "scale_factor": [100, 150, 200, 300, 500],
            "min_divergence": [0.02, 0.04, 0.06, 0.08],
            "time_weight_ramp_sec": [60, 90, 120, 180],
            "min_edge_bps": [100, 150, 200, 300],
        },
        "contrarian_extreme_fade": {
            "extreme_threshold_high": [0.72, 0.75, 0.78, 0.82, 0.85],
            "damping": [0.4, 0.5, 0.6, 0.7, 0.8],
            "max_elapsed_for_fade": [60, 90, 120],
            "spot_confirmation_weight": [0.0, 0.15, 0.3, 0.5],
        },
        "volatility_regime": {
            "vol_low_threshold": [5, 8, 12, 18],
            "vol_high_threshold": [25, 35, 50],
            "logistic_scale": [0.01, 0.02, 0.03],
            "high_vol_size_multiplier": [1.0, 1.3, 1.5, 2.0],
        },
        "order_book_imbalance": {
            "obi_threshold": [0.15, 0.20, 0.25, 0.35, 0.45],
            "beta": [0.05, 0.08, 0.10, 0.15, 0.20],
            "alpha": [0.1, 0.2, 0.3, 0.5],
            "min_edge_bps": [80, 120, 150, 200],
        },
    }

--- 6.2 Backtesting Methodology ---

Step 1: Data Collection
    - Use data.fetcher.discover_resolved_markets() to find recent resolved markets.
    - Need at minimum 100 windows per asset (300 total) for statistical significance.
    - Ensure coverage across different times of day (Asian, European, US sessions)
      and different volatility regimes.
    - Store raw events (spot ticks + CLOB snapshots) for deterministic replay.

Step 2: Walk-Forward Validation
    - Split data into 3 folds (chronological, NOT random):
        Fold 1: Train (first 40%), Validate (next 20%), Test (last 40%)
        Fold 2: Train (first 60%), Test (last 40%) [different test split]
    - Optimize parameters on Train, select on Validate, report on Test.
    - NEVER touch Test data during parameter selection.

Step 3: Key Metrics to Track
    Primary:
        - Net PnL (after fees)
        - Sharpe-like ratio: mean(PnL per trade) / std(PnL per trade)
        - Win rate
        - Profit factor
        - Max drawdown (absolute and as % of bankroll)
    Secondary:
        - Trade frequency (trades per hour)
        - Average edge realized vs. predicted
        - Edge decay curve: how quickly does edge disappear after signal fires?
        - Per-asset breakdown (BTC vs ETH vs SOL)
        - Time-of-day performance
    Diagnostic:
        - Signal accuracy: what % of "p_hat > midpoint" calls are correct?
        - Fee drag: total fees / gross profit
        - Spread cost: total slippage / gross profit
        - Fill rate: % of signals that get filled

Step 4: Statistical Significance Tests
    - Null hypothesis: strategy has zero edge (PnL comes from luck).
    - Test: one-sided t-test on per-trade PnL > 0.
    - Required: p-value < 0.05 on out-of-sample test set.
    - Additionally: bootstrap the PnL series (10,000 samples) to get confidence
      intervals on total PnL.

Step 5: Overfitting Controls
    - Use Bonferroni correction for multiple strategy/parameter comparisons.
    - If testing K parameter combinations, required p-value = 0.05 / K.
    - For 100 combinations: need p < 0.0005 on out-of-sample.
    - Track "out-of-sample decay": how much worse is test PnL vs train PnL?
      If test PnL < 50% of train PnL, likely overfitting.

--- 6.3 Benchmark Comparison Protocol ---

Every strategy MUST beat these benchmarks to be considered viable:

    1. AlwaysYes: buys YES every window at 50 contracts.
       Expected PnL: slightly negative (fees eat the 50/50 payoff).

    2. AlwaysNo: buys NO every window at 50 contracts.
       Expected PnL: slightly negative.

    3. RandomStrategy: random entry.
       Expected PnL: negative (fees).

    4. SpotMomentumStrategy: the existing baseline.
       This is the bar to beat.

A new strategy is interesting only if:
    - It beats SpotMomentum on net PnL AND
    - It beats SpotMomentum on Sharpe ratio AND
    - The improvement is statistically significant (p < 0.05)

Or if it is UNCORRELATED with SpotMomentum (low signal overlap), in which
case it adds value in an ensemble even if standalone PnL is lower.


================================================================================
PART VII: ENSEMBLE / STRATEGY COMBINATION FRAMEWORK
================================================================================

--- 7.1 Why Ensembles ---

Individual strategies have different strengths:
    - FeeOptimizedExtremes: high accuracy at extremes, low frequency
    - TimeDecayBias: strong late-window, moderate frequency
    - SpotMomentum: moderate edge at moderate prices, high frequency
    - ContrarianExtremeFade: contrarian, low frequency, high payoff

Running them together (on different windows) or combining their signals
(on the same window) can improve Sharpe ratio through diversification.

--- 7.2 Ensemble Architecture ---

Option A: Signal Averaging (Soft Ensemble)
    Run all strategies on each window. Average their p_hat estimates:
    p_ensemble = sum(w_i * p_hat_i) / sum(w_i)
    where w_i are strategy weights (proportional to historical Sharpe).

    Only trade if p_ensemble has sufficient edge AND at least N strategies
    agree on direction (unanimous vote or majority vote).

    Pros: smooths out noise, reduces false positives.
    Cons: waters down strong signals, adds complexity.

Option B: Strategy Rotation (Hard Ensemble)
    Run exactly ONE strategy per window, selected based on current conditions:
    - If midpoint at extreme (> 0.78 or < 0.22): use FeeOptimizedExtremes
    - If elapsed > 150s: use TimeDecayBias
    - If vol > threshold: use VolatilityRegime with boosted sizing
    - Default: use SpotMomentum or SpotPredictionDivergence

    Pros: simple, no signal conflict, each strategy in its optimal zone.
    Cons: handoff logic can be fragile, need to define zone boundaries.

Option C: Independent Parallel (Portfolio Ensemble)
    Run all strategies independently, each with their own bankroll allocation:
    - FeeOptimizedExtremes: 30% of bankroll
    - TimeDecayBias: 25% of bankroll
    - SpotPredictionDivergence: 25% of bankroll
    - ContrarianExtremeFade: 20% of bankroll

    Each trades independently. The runner prevents conflicts by not allowing
    multiple positions in the same market (already enforced by OrderManager).

    Pros: truly independent, easy to implement, natural diversification.
    Cons: some windows get no trades (if no strategy fires), capital may
    sit idle.

RECOMMENDATION: Start with Option B (Strategy Rotation). It is the simplest
to implement and debug. The rotation logic can be encoded as a meta-strategy:

    class RotationStrategy(BaseStrategy):
        name = "rotation"

        def __init__(self, strategies: List[BaseStrategy]):
            self.strategies = strategies

        def evaluate(self, state: MarketState) -> Signal:
            # Select the best strategy for current conditions
            if state.midpoint > 0.78 or state.midpoint < 0.22:
                return self.strategies["fee_extremes"].evaluate(state)
            elif state.elapsed_sec > 150:
                return self.strategies["time_decay"].evaluate(state)
            else:
                return self.strategies["spot_momentum"].evaluate(state)


================================================================================
PART VIII: RISK CONSIDERATIONS & FAILURE MODES
================================================================================

--- 8.1 Systematic Risks ---

1. Fee Structure Change
    If Polymarket changes the fee formula, all fee-dependent strategies
    (especially #12, #10) lose their structural advantage. Monitor the
    docs page and have a kill switch.

2. Market Maker Competition
    If sophisticated market makers enter Polymarket's crypto markets, the
    prediction market will become more efficient. Edges from divergence
    (Strategy 3) and OBI (Strategy 1) will shrink. The time to extract
    value is NOW, while the market is still retail-dominated.

3. Latency
    Our execution path: detect signal -> evaluate strategy -> simulate fill
    -> place order -> order reaches CLOB. If total latency > 2-3 seconds,
    many microstructure-based signals will have evaporated. Monitor fill
    rates and slippage closely.

4. Spot Feed Reliability
    All strategies depend on the Coinbase WebSocket spot feed. If the feed
    drops, stale spot data will generate spurious signals. Implement a
    staleness check: if spot_price timestamp is > 3 seconds old, refuse
    to trade.

5. Correlation Regime Shift
    Strategy 6 (CrossAssetCorrelation) assumes stable BTC/ETH/SOL
    correlations. During market stress or idiosyncratic events (e.g., ETH
    merge, SOL outage), correlations can flip. Need rolling correlation
    monitoring and a circuit breaker.

--- 8.2 Strategy-Specific Failure Modes ---

FeeOptimizedExtremes:
    FAILURE: Polymarket reduces fees across the board -> edge disappears.
    FAILURE: Only trades during high-vol windows -> sequences of losses
             in trending markets that don't revert.
    MITIGATION: Combine with vol regime filter; track rolling win rate
                and pause if < 40% over last 20 trades.

TimeDecayBias:
    FAILURE: Market correctly prices time decay -> no edge.
    FAILURE: Spot reversal in last 90 seconds wipes out position.
    MITIGATION: Use the Brownian motion model (Phi-based) instead of
                logistic for more accurate late-window probabilities.
                Track spot reversal frequency in last 90s.

ContrarianExtremeFade:
    FAILURE: Strong trend market where extremes are correct.
    FAILURE: Buying cheap NO at 0.18, but outcome IS yes 82% of the time.
    MITIGATION: Only fade when spot return has decelerated (combine with
                Strategy 9: MomentumAcceleration). If acceleration is still
                positive, don't fade.

--- 8.3 Portfolio-Level Risk Controls ---

The existing RiskEngine covers:
    - Max position per market (size limit)
    - Max total exposure (notional limit)
    - Max concurrent positions
    - Max drawdown circuit breaker
    - Cooldown after loss

Additional controls to implement:

    1. Per-Strategy Loss Limit:
       Track PnL per strategy. If a strategy loses > $X in a session,
       disable it for the rest of the session. This prevents a
       malfunctioning strategy from burning through the bankroll.

    2. Correlation-Based Position Limit:
       If holding YES on BTC and YES on ETH (highly correlated), the
       effective risk is ~2x a single position. Limit correlated positions
       to max 2 at a time (e.g., don't hold YES on BTC, ETH, AND SOL
       simultaneously).

    3. Realized Edge Tracking:
       Track predicted edge (ev_bps) vs realized outcome. If the average
       realized edge is < 50% of predicted edge over 50+ trades, the model
       is miscalibrated. Trigger a recalibration or pause.


================================================================================
PART IX: DATA REQUIREMENTS & COLLECTION PLAN
================================================================================

--- 9.1 Data Needed for Backtesting ---

    Type              | Source              | Frequency      | Storage
    ------------------|--------------------|--------------  |--------
    Spot prices       | Coinbase WS        | ~100ms ticks   | ~50MB/day
    CLOB snapshots    | Polymarket CLOB WS | ~1-5s updates  | ~20MB/day
    Market metadata   | Polymarket REST    | per window     | <1MB/day
    Resolutions       | Polymarket REST    | per window     | <1MB/day

    Total: ~70MB/day, ~2GB/month.

    Minimum for meaningful backtest: 7 days (300+ windows per asset).
    Recommended: 30 days (1500+ windows per asset) for robust statistics.

--- 9.2 Calibration Data ---

    Strategies 3 and 7 require calibration of the logistic scale parameter.
    This needs a labeled dataset:
        (spot_return_bps_at_time_t, elapsed_sec, outcome)
    for thousands of windows. Build by replaying historical data and
    labeling each tick with the window's final outcome.

    Run logistic regression:
        log(p/(1-p)) = beta_0 + beta_1 * spot_return_bps + beta_2 * elapsed_sec
                       + beta_3 * spot_return_bps * elapsed_sec

    The interaction term beta_3 captures exactly the "time decay" effect:
    as elapsed_sec increases, the coefficient on spot_return_bps should grow
    (same return is more predictive later). This directly validates Strategy 7.

--- 9.3 Cross-Asset Data ---

    Strategy 6 needs simultaneous spot prices for BTC, ETH, SOL.
    The current runner tracks these (self._spot_prices dict), but they
    are not exposed to strategies.

    Also need: historical 5-minute correlation matrices, computed from
    spot data. Recalibrate weekly.

--- 9.4 Collection Pipeline ---

    1. Start recording: run data collection in background 24/7.
    2. After 7 days: run initial backtests on Tier 1 strategies.
    3. After 14 days: run parameter sweeps, select best configs.
    4. After 21 days: out-of-sample validation on last 7 days.
    5. After 30 days: if profitable, begin paper trading.

    The data collector should store events in the same format as
    data.models.Event so that data.replay_source can replay them
    directly through the StrategyRunner.


================================================================================
PRIORITY RANKING (by expected feasibility x edge)
================================================================================

Tier 1 (implement first -- simple, high expected edge):
    1. FeeOptimizedExtremes (#12) -- exploits structural fee advantage, simple
    2. TimeDecayBias (#7) -- clear mathematical basis, uses existing data
    3. SpotPredictionDivergence (#3) -- refined version of existing strategy

Tier 2 (implement next -- moderate complexity, good edge):
    4. ContrarianExtremeFade (#10) -- fee advantage + mean reversion
    5. VolatilityRegime (#4) -- smart filter on existing signal
    6. OrderBookImbalance (#1) -- well-studied microstructure signal

Tier 3 (implement later -- require more infra or have uncertain edge):
    7. DepthWeightedFairValue (#5) -- needs raw book data in MarketState
    8. MomentumAcceleration (#9) -- noise amplification risk
    9. SpotBookConsensus (#11) -- complex, needs multi-signal calibration
    10. LiquidityVacuum (#8) -- needs reliable depth data
    11. SpreadCompression (#2) -- needs high tick frequency
    12. CrossAssetCorrelation (#6) -- needs cross-market state infrastructure


================================================================================
END OF RESEARCH DOCUMENT
================================================================================
"""

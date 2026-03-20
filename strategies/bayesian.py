"""
Bayesian probability estimator for Polymarket 5-min binary markets.

Replaces the logistic transform used by spot_momentum and other strategies.
The logistic approach has a critical flaw: it only uses CUMULATIVE spot return,
ignoring the PATH. A stock that went +50 bps then -45 bps (net +5 bps) looks
the same as one that drifted +5 bps smoothly. The Bayesian approach captures
the path by accumulating evidence tick-by-tick via a Beta-Bernoulli conjugate
pair.

Two usage modes:
  1. BayesianProbEstimator — stateful, feed it ticks within a 5-min window.
  2. quick_bayesian_p()   — stateless, takes a MarketState snapshot and
     approximates the sequential result. This is what strategies use in
     the current backtest architecture (one snapshot per CLOB event).
"""

import math
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Stateful sequential estimator
# ---------------------------------------------------------------------------

class BayesianProbEstimator:
    """
    Beta-Bernoulli conjugate updater for P(up) within a 5-min window.

    Each incoming tick adds fractional pseudo-counts to alpha (evidence for up)
    or beta (evidence for down). Weights scale with elapsed time so that later
    ticks — which carry more information about the final outcome — contribute
    more to the posterior.

    The posterior mean  alpha / (alpha + beta)  is the probability estimate.
    """

    def __init__(self, time_weight_gamma: float = 1.5):
        """
        Args:
            time_weight_gamma: Controls how much later ticks matter more.
                >1  = convex acceleration (later ticks dominate)
                 1  = linear weighting
                <1  = concave (early ticks matter more)
        """
        self.time_weight_gamma: float = time_weight_gamma
        self.alpha: float = 1.0          # Beta prior: starts uniform (1,1)
        self.beta: float = 1.0
        self.last_price: Optional[float] = None
        self.last_obi: Optional[float] = None
        self.window_start_ts: Optional[int] = None   # unix ms
        self.window_duration: int = 300               # seconds

    # -- tick-level updaters ------------------------------------------------

    def update_spot(self, price: float, ts_ms: int) -> None:
        """
        Update with a new spot price tick.

        Each tick is compared to the previous price. An uptick adds fractional
        pseudo-counts to alpha; a downtick adds to beta. The magnitude scales
        with both the tick size and a time weight that increases toward the end
        of the window (controlled by time_weight_gamma).
        """
        if self.window_start_ts is None:
            self.window_start_ts = ts_ms

        if self.last_price is None:
            self.last_price = price
            return

        delta_bps = (price - self.last_price) / self.last_price * 10_000
        self.last_price = price

        if delta_bps == 0.0:
            return

        # Time weight: (elapsed / total)^gamma,  clipped to [0.01, 1.0]
        elapsed_sec = (ts_ms - self.window_start_ts) / 1000.0
        frac = max(0.0, min(1.0, elapsed_sec / self.window_duration))
        w_time = max(0.01, frac ** self.time_weight_gamma)

        # Magnitude weight: each bps of move contributes proportionally.
        # Scale so that a 10 bps tick ~ 0.1 pseudo-count before time weight.
        w_mag = abs(delta_bps) / 100.0

        increment = w_time * w_mag

        if delta_bps > 0:
            self.alpha += increment
        else:
            self.beta += increment

    def update_obi(self, obi: float, weight: float = 0.05) -> None:
        """
        Update with order book imbalance observation.

        Only the CHANGE in OBI since the last call is used, to avoid
        double-counting when the same book snapshot is observed repeatedly.

        Args:
            obi: Order book imbalance in [-1, +1].
                 Positive = more bids (bullish), negative = more asks (bearish).
            weight: Scaling factor. Keep small — OBI is noisy on thin books.
        """
        if self.last_obi is None:
            self.last_obi = obi
            return

        delta_obi = obi - self.last_obi
        self.last_obi = obi

        if delta_obi == 0.0:
            return

        increment = abs(delta_obi) * weight

        if delta_obi > 0:
            self.alpha += increment
        else:
            self.beta += increment

    def update_microprice(self, microprice: float, midpoint: float,
                          weight: float = 0.03) -> None:
        """
        Update with microprice divergence from midpoint.

        microprice > midpoint signals upward pressure (add to alpha).
        microprice < midpoint signals downward pressure (add to beta).

        Args:
            microprice: Depth-weighted fair value.
            midpoint: Simple (best_bid + best_ask) / 2.
            weight: Scaling factor. Keep small since the signal is subtle.
        """
        if midpoint <= 0:
            return

        divergence_bps = (microprice - midpoint) / midpoint * 10_000
        if divergence_bps == 0.0:
            return

        increment = abs(divergence_bps) / 100.0 * weight

        if divergence_bps > 0:
            self.alpha += increment
        else:
            self.beta += increment

    # -- posterior queries --------------------------------------------------

    @property
    def p_hat(self) -> float:
        """Current posterior mean P(up)."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def confidence(self) -> float:
        """
        Confidence measure: how much evidence has been accumulated.

        Returns a value in [0, 1]. Starts at 0 (just the prior) and
        approaches 1 after ~20 pseudo-counts of total evidence.
        Computed as min(1.0, (alpha + beta - 2) / 20).
        """
        total_evidence = self.alpha + self.beta - 2.0  # subtract the prior
        return min(1.0, max(0.0, total_evidence / 20.0))

    @property
    def uncertainty(self) -> float:
        """
        Posterior standard deviation of P(up).

        For a Beta(alpha, beta) distribution:
            Var = alpha*beta / ((alpha+beta)^2 * (alpha+beta+1))
            SD  = sqrt(Var)
        """
        a, b = self.alpha, self.beta
        ab = a + b
        variance = (a * b) / (ab * ab * (ab + 1.0))
        return math.sqrt(variance)

    # -- lifecycle ----------------------------------------------------------

    def reset(self, window_start_ts: int = None) -> None:
        """Reset for a new 5-min window."""
        self.alpha = 1.0
        self.beta = 1.0
        self.last_price = None
        self.last_obi = None
        self.window_start_ts = window_start_ts


# ---------------------------------------------------------------------------
# Kelly criterion with fee awareness
# ---------------------------------------------------------------------------

def bayesian_kelly(p_hat: float, confidence: float, entry_price: float,
                   base_fraction: float = 0.25) -> float:
    """
    Confidence-weighted Kelly criterion for Polymarket binary contracts.

    Standard Kelly:  f* = (p*b - q) / b
        where b = payout odds = (1 - entry_price) / entry_price
              q = 1 - p

    Adjustments applied:
      1. Fee awareness — effective cost is higher than entry_price.
      2. Confidence scaling — scale down when evidence is thin.
      3. Base fraction — fractional Kelly for risk management.

    Args:
        p_hat: Estimated probability of YES outcome (0, 1).
        confidence: Confidence level in [0, 1] from the estimator.
        entry_price: Price we would pay for the contract (0, 1).
        base_fraction: Fractional Kelly multiplier (default 0.25 = quarter Kelly).

    Returns:
        Optimal fraction of bankroll to wager, in [0, 1].
    """
    if p_hat <= 0.0 or p_hat >= 1.0 or entry_price <= 0.0 or entry_price >= 1.0:
        return 0.0

    # Fee-adjusted effective cost: Polymarket taker fee formula
    # fee_rate_eff = 0.25 * (p * (1-p))^2,  applied to notional = price * size
    # Per-contract fee = price * fee_rate * (price*(1-price))^2
    fee_rate = 0.25
    exponent = 2
    per_unit_fee = entry_price * fee_rate * (entry_price * (1.0 - entry_price)) ** exponent
    eff_cost = entry_price + per_unit_fee

    if eff_cost >= 1.0:
        return 0.0

    # Payout odds: pay eff_cost, receive 1.0 on win
    b = (1.0 - eff_cost) / eff_cost
    q = 1.0 - p_hat

    # Standard Kelly
    f_star = (p_hat * b - q) / b
    if f_star <= 0.0:
        return 0.0

    # Confidence scaling: ramp from 0 to 1 as evidence accumulates
    conf_factor = min(1.0, max(0.0, confidence))

    # Final fraction
    f = f_star * conf_factor * base_fraction
    return max(0.0, min(1.0, f))


# ---------------------------------------------------------------------------
# Stateless convenience for current backtest architecture
# ---------------------------------------------------------------------------

def quick_bayesian_p(state) -> Tuple[float, float]:
    """
    Stateless Bayesian probability estimate from a single MarketState snapshot.

    Approximates what the full sequential BayesianProbEstimator would produce
    by converting available MarketState fields into alpha/beta pseudo-counts.

    The key improvement over the old logistic approach: we incorporate time
    weighting, OBI, and microprice — not just cumulative spot return.

    Args:
        state: A MarketState dataclass (from strategies.base).

    Returns:
        (p_hat, confidence) — estimated P(up) and confidence level [0, 1].
    """
    alpha = 1.0
    beta = 1.0

    # --- 1. Spot return evidence ---
    # Scale: 10 bps of return ~ 0.1 pseudo-counts (before time weight).
    spot_bps = getattr(state, "spot_return_bps", 0.0) or 0.0
    elapsed = getattr(state, "elapsed_sec", 0) or 0
    window_dur = 300.0

    # Time weight: later in the window = more informative
    frac = max(0.0, min(1.0, elapsed / window_dur))
    w_time = max(0.01, frac ** 1.5)  # gamma = 1.5

    # Magnitude: scale so 10 bps ~ 0.1 base count
    spot_increment = abs(spot_bps) / 100.0 * w_time

    # To approximate the PATH effect from a single snapshot, we scale
    # the evidence by the square root of elapsed time. The intuition:
    # a smooth drift accumulates evidence proportional to sqrt(T) (like
    # a random walk's displacement), while a choppy path with reversals
    # would accumulate less net evidence. Since we only see the net move,
    # sqrt scaling is a conservative middle ground.
    path_scale = math.sqrt(max(1.0, elapsed)) / math.sqrt(window_dur)
    spot_increment *= path_scale

    if spot_bps > 0:
        alpha += spot_increment
    elif spot_bps < 0:
        beta += spot_increment

    # --- 2. OBI adjustment ---
    obi = getattr(state, "obi", 0.0) or 0.0
    obi_weight = 0.05
    obi_increment = abs(obi) * obi_weight
    if obi > 0:
        alpha += obi_increment
    elif obi < 0:
        beta += obi_increment

    # --- 3. Microprice divergence ---
    microprice = getattr(state, "microprice", 0.0) or 0.0
    midpoint = getattr(state, "midpoint", 0.0) or 0.0
    if midpoint > 0 and microprice > 0:
        div_bps = (microprice - midpoint) / midpoint * 10_000
        mp_increment = abs(div_bps) / 100.0 * 0.03
        if div_bps > 0:
            alpha += mp_increment
        elif div_bps < 0:
            beta += mp_increment

    # --- Posterior ---
    p_hat = alpha / (alpha + beta)

    # Confidence: how much total evidence beyond the prior
    total_evidence = alpha + beta - 2.0
    confidence = min(1.0, max(0.0, total_evidence / 20.0))

    return (p_hat, confidence)

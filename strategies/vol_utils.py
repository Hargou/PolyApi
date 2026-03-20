"""
Volatility utilities for 5-minute binary crypto markets.

The naive tick-to-tick return variance used in time_decay.py and
volatility_regime.py is the *least* efficient estimator: it only uses
close-to-close information and ignores the high/low range within each
sub-period.  Range-based estimators (Parkinson, Yang-Zhang) extract ~5x
more information from the same OHLC data.

This module provides:
  1. parkinson_vol      – range-based estimator (H/L only)
  2. yang_zhang_vol     – full OHLC estimator (most accurate)
  3. TickVolTracker     – stateful per-window tracker, builds sub-interval
                          OHLC bars from raw ticks and serves vol estimates
  4. implied_vol_from_market – back out sigma from a Polymarket price
  5. vol_adjusted_p_hat – Brownian P(up) with a proper vol input

All functions are pure Python (math + statistics stdlib).  scipy is used
for the normal CDF when available, otherwise a fast math.erf fallback.
"""

from __future__ import annotations

import math
import statistics
from typing import List, Optional

# ---------------------------------------------------------------------------
# Normal CDF — prefer scipy, fall back to math.erf
# ---------------------------------------------------------------------------

try:
    from scipy.stats import norm as _scipy_norm

    def phi(x: float) -> float:
        """Standard normal CDF."""
        return float(_scipy_norm.cdf(x))

    def phi_pdf(x: float) -> float:
        """Standard normal PDF (needed for Newton-Raphson)."""
        return float(_scipy_norm.pdf(x))

except ImportError:

    def phi(x: float) -> float:
        """Standard normal CDF (Abramowitz & Stegun via math.erf)."""
        if x > 8.0:
            return 1.0
        if x < -8.0:
            return 0.0
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def phi_pdf(x: float) -> float:
        """Standard normal PDF."""
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ---------------------------------------------------------------------------
# 1.  Parkinson (1980) range-based volatility estimator
# ---------------------------------------------------------------------------

def parkinson_vol(highs: List[float], lows: List[float]) -> float:
    """Parkinson range-based volatility estimator.

    Uses the high-low range of each sub-period rather than just the close.
    Roughly 5x more efficient than close-to-close variance for a continuous
    GBM process.

    Formula
    -------
    sigma^2 = (1 / (4 * n * ln(2))) * sum( (ln(H_i / L_i))^2 )

    Parameters
    ----------
    highs : list of float
        Sub-period high prices.
    lows : list of float
        Sub-period low prices (same length as *highs*).

    Returns
    -------
    float
        Volatility (per sub-period) as a positive number.
        To convert to bps multiply by 10_000.
        Returns 0.0 if inputs are empty or degenerate.
    """
    n = len(highs)
    if n == 0 or n != len(lows):
        return 0.0

    sum_sq = 0.0
    valid = 0
    for h, l in zip(highs, lows):
        if l <= 0.0 or h < l:
            continue
        log_hl = math.log(h / l)
        sum_sq += log_hl * log_hl
        valid += 1

    if valid == 0:
        return 0.0

    variance = sum_sq / (4.0 * valid * math.log(2.0))
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# 2.  Yang-Zhang (2000) volatility estimator
# ---------------------------------------------------------------------------

def yang_zhang_vol(
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
) -> float:
    """Yang-Zhang OHLC volatility estimator.

    Combines three components:
      - Overnight (close-to-open) variance
      - Open-to-close variance
      - Rogers-Satchell intra-bar variance

    It is the minimum-variance unbiased estimator for a process with both
    drift and jumps, making it the most accurate range-based estimator.

    Formula
    -------
    sigma_yz^2 = sigma_o^2 + k * sigma_c^2 + (1 - k) * sigma_rs^2
    where k = 0.34 / (1.34 + (n+1)/(n-1))

    For *intra-day sub-intervals* where there is no overnight gap, the
    overnight component uses the previous bar's close as the "open" of
    the overnight move.  When there is only a single bar the function
    falls back to Parkinson.

    Parameters
    ----------
    opens, highs, lows, closes : list of float
        OHLC prices for each sub-period.  All four lists must have the
        same length (>= 2 for a meaningful estimate).

    Returns
    -------
    float
        Per-sub-period volatility (fraction, not bps).
    """
    n = len(opens)
    if n < 2 or len(highs) != n or len(lows) != n or len(closes) != n:
        # Not enough bars — fall back to Parkinson if we have H/L
        if n >= 1 and len(highs) == n and len(lows) == n:
            return parkinson_vol(highs, lows)
        return 0.0

    # --- Overnight variance (previous close -> current open) ---
    log_oc_overnight: List[float] = []
    for i in range(1, n):
        if closes[i - 1] > 0.0 and opens[i] > 0.0:
            log_oc_overnight.append(math.log(opens[i] / closes[i - 1]))

    if not log_oc_overnight:
        return parkinson_vol(highs, lows)

    mean_on = sum(log_oc_overnight) / len(log_oc_overnight)
    sigma_o_sq = sum((x - mean_on) ** 2 for x in log_oc_overnight) / (len(log_oc_overnight) - 1) if len(log_oc_overnight) > 1 else 0.0

    # --- Close-to-close (open->close of each bar) variance ---
    log_co: List[float] = []
    for i in range(n):
        if opens[i] > 0.0 and closes[i] > 0.0:
            log_co.append(math.log(closes[i] / opens[i]))

    if len(log_co) < 2:
        return parkinson_vol(highs, lows)

    mean_co = sum(log_co) / len(log_co)
    sigma_c_sq = sum((x - mean_co) ** 2 for x in log_co) / (len(log_co) - 1)

    # --- Rogers-Satchell variance ---
    rs_terms: List[float] = []
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if o <= 0.0 or h <= 0.0 or l <= 0.0 or c <= 0.0:
            continue
        if h < l:
            continue
        log_ho = math.log(h / o)
        log_hc = math.log(h / c)
        log_lo = math.log(l / o)
        log_lc = math.log(l / c)
        rs_terms.append(log_ho * log_hc + log_lo * log_lc)

    if not rs_terms:
        return parkinson_vol(highs, lows)

    sigma_rs_sq = sum(rs_terms) / len(rs_terms)

    # --- Yang-Zhang combination ---
    k = 0.34 / (1.34 + (n + 1.0) / (n - 1.0))
    sigma_yz_sq = sigma_o_sq + k * sigma_c_sq + (1.0 - k) * sigma_rs_sq

    # Guard against floating-point noise producing a tiny negative
    if sigma_yz_sq <= 0.0:
        return parkinson_vol(highs, lows)

    return math.sqrt(sigma_yz_sq)


# ---------------------------------------------------------------------------
# 3.  TickVolTracker — stateful per-window vol tracker
# ---------------------------------------------------------------------------

class TickVolTracker:
    """Accumulates raw ticks during a 5-min window and provides range-based
    vol estimates.

    Ticks are bucketed into fixed-length sub-intervals (default 30 s,
    giving ~10 bars per 5-min window).  OHLC bars are built on the fly
    and fed to :func:`yang_zhang_vol`.

    Usage::

        tracker = TickVolTracker(sub_interval_sec=30)
        for price, ts_ms in stream:
            tracker.add_tick(price, ts_ms)
            vol = tracker.realized_vol_bps()
            regime = tracker.vol_regime()
        tracker.reset()
    """

    def __init__(self, sub_interval_sec: int = 30):
        self.sub_interval_ms: int = sub_interval_sec * 1000
        self.reset()

    # ---- public API ----

    def add_tick(self, price: float, ts_ms: int) -> None:
        """Record a price tick with its timestamp (unix milliseconds)."""
        if price <= 0.0:
            return

        self._all_prices.append(price)
        self._all_ts.append(ts_ms)

        # Determine which sub-interval this tick belongs to
        if self._bar_start_ms is None:
            # First tick ever — start the first bar
            self._bar_start_ms = ts_ms
            self._bar_open = price
            self._bar_high = price
            self._bar_low = price
            self._bar_close = price
            return

        elapsed_in_bar = ts_ms - self._bar_start_ms

        if elapsed_in_bar < self.sub_interval_ms:
            # Still in the current bar
            self._bar_high = max(self._bar_high, price)
            self._bar_low = min(self._bar_low, price)
            self._bar_close = price
        else:
            # Finalize the current bar
            self._finalize_bar()

            # Skip any completely empty intermediate bars (unlikely for
            # crypto but safe).  Just start a new bar at the current tick.
            self._bar_start_ms = ts_ms
            self._bar_open = price
            self._bar_high = price
            self._bar_low = price
            self._bar_close = price

    def realized_vol_bps(self) -> float:
        """Current realized vol in basis points, using Yang-Zhang on
        completed sub-interval bars.

        Returns 0.0 if fewer than 2 bars have completed.
        """
        bars = self._bars
        if len(bars) < 2:
            return 0.0

        opens  = [b[0] for b in bars]
        highs  = [b[1] for b in bars]
        lows   = [b[2] for b in bars]
        closes = [b[3] for b in bars]

        vol_frac = yang_zhang_vol(opens, highs, lows, closes)
        return vol_frac * 10_000.0

    def tick_vol_bps(self) -> float:
        """Fallback: simple tick-to-tick return standard deviation in bps.

        This is the same estimator the old strategies used.  Kept for
        comparison / fallback when not enough bars are available.
        """
        prices = self._all_prices
        if len(prices) < 3:
            return 0.0

        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0.0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1] * 10_000.0)

        if len(returns) < 2:
            return 0.0

        return statistics.stdev(returns)

    def vol_regime(self, baseline_vol_bps: float = 40.0) -> str:
        """Classify the current volatility regime.

        Uses :meth:`realized_vol_bps` when enough bars are available,
        otherwise falls back to :meth:`tick_vol_bps`.

        Parameters
        ----------
        baseline_vol_bps : float
            The "normal" 5-min vol expectation in bps.  Default 40 bps
            is a reasonable starting point for BTC.

        Returns
        -------
        str
            One of ``'CALM'``, ``'NORMAL'``, or ``'STORM'``.

            * **CALM** (< 0.5x baseline): skip, no edge — prices stay
              near 50 % and fees eat everything.
            * **NORMAL** (0.5x–2.0x baseline): trade with base Kelly.
            * **STORM** (> 2.0x baseline): aggressive sizing — prices
              pushed to extremes where fees are cheap.
        """
        vol = self.realized_vol_bps()
        if vol == 0.0:
            vol = self.tick_vol_bps()
        if vol == 0.0:
            return "CALM"  # no data → conservative default

        ratio = vol / baseline_vol_bps
        if ratio < 0.5:
            return "CALM"
        if ratio > 2.0:
            return "STORM"
        return "NORMAL"

    def reset(self) -> None:
        """Reset all state for a new window."""
        self._all_prices: List[float] = []
        self._all_ts: List[int] = []
        # Completed OHLC bars: list of (open, high, low, close) tuples
        self._bars: List[tuple] = []
        # In-progress bar state
        self._bar_start_ms: Optional[int] = None
        self._bar_open: float = 0.0
        self._bar_high: float = 0.0
        self._bar_low: float = 0.0
        self._bar_close: float = 0.0

    # ---- internals ----

    def _finalize_bar(self) -> None:
        """Push the in-progress bar onto the completed bars list."""
        if self._bar_start_ms is not None and self._bar_open > 0.0:
            self._bars.append((
                self._bar_open,
                self._bar_high,
                self._bar_low,
                self._bar_close,
            ))

    @property
    def bar_count(self) -> int:
        """Number of completed sub-interval bars."""
        return len(self._bars)

    @property
    def tick_count(self) -> int:
        """Total number of ticks received."""
        return len(self._all_prices)


# ---------------------------------------------------------------------------
# 4.  Implied volatility from Polymarket price
# ---------------------------------------------------------------------------

def implied_vol_from_market(
    p_market: float,
    spot_return_bps: float,
    remaining_sec: int,
) -> float:
    """Back out the market's implied volatility from a Polymarket price.

    Under the Brownian/binary-option model:

        P(up) = Phi( spot_return / (sigma * sqrt(T)) )

    Given the observed *p_market* (Polymarket midpoint) and the current
    *spot_return_bps*, we invert Phi via Newton-Raphson to solve for sigma.

    Parameters
    ----------
    p_market : float
        Polymarket probability for YES (0–1).
    spot_return_bps : float
        Current spot return since window start, in bps.
    remaining_sec : int
        Seconds remaining in the window.

    Returns
    -------
    float
        Implied vol in bps.  Returns 0.0 if the inputs are degenerate
        (e.g., p_market at the boundary, or remaining_sec <= 0).
    """
    # Guard rails
    p = max(0.01, min(0.99, p_market))
    if remaining_sec <= 0:
        return 0.0
    if abs(spot_return_bps) < 0.01:
        # Zero spot return → can't back out sigma (Phi(0) = 0.5 for any sigma)
        return 0.0

    remaining_min = remaining_sec / 60.0
    sqrt_t = math.sqrt(remaining_min)

    # Target: find sigma such that Phi(spot_return_bps / (sigma * sqrt_t)) = p
    # Equivalently: z_target = Phi^{-1}(p)  =>  sigma = spot_return_bps / (z_target * sqrt_t)
    #
    # But Phi^{-1} (the quantile / probit function) can be computed directly.
    # We use Newton-Raphson on f(z) = Phi(z) - p.  z_target is the root.
    # Then sigma_bps = spot_return_bps / (z_target * sqrt_t).

    # Newton-Raphson for z_target = Phi^{-1}(p)
    # Start from a decent initial guess using the rational approximation.
    z = _probit_approx(p)

    for _ in range(5):
        f_val = phi(z) - p
        f_deriv = phi_pdf(z)
        if f_deriv < 1e-15:
            break
        z -= f_val / f_deriv

    if abs(z) < 1e-10:
        return 0.0

    sigma_bps = spot_return_bps / (z * sqrt_t)

    # sigma should be positive.  If the sign of the return and z disagree
    # (market price contradicts spot direction), sigma comes out negative.
    # Return absolute value — the market is pricing some vol, just in the
    # opposite-direction scenario.
    return abs(sigma_bps)


def _probit_approx(p: float) -> float:
    """Approximate Phi^{-1}(p) (probit function) for Newton seeding.

    Uses the Beasley-Springer-Moro rational approximation which is
    accurate to ~1e-9 for 0.02 < p < 0.98.  Outside that range we
    clip to +/- 3 (good enough for a Newton seed).
    """
    if p <= 0.02:
        return -3.0
    if p >= 0.98:
        return 3.0

    # Abramowitz & Stegun 26.2.23 — simpler rational form
    if p < 0.5:
        t = math.sqrt(-2.0 * math.log(p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        return -(t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t))
    else:
        t = math.sqrt(-2.0 * math.log(1.0 - p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        return t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t)


# ---------------------------------------------------------------------------
# 5.  Brownian P(up) with proper vol
# ---------------------------------------------------------------------------

def vol_adjusted_p_hat(
    spot_return_bps: float,
    vol_bps: float,
    remaining_sec: float,
) -> float:
    """Brownian-motion P(up) using a *measured* vol instead of a hard-coded
    baseline.

    Formula
    -------
    P(up) = Phi( spot_return_bps / (vol_bps * sqrt(remaining_min)) )

    Parameters
    ----------
    spot_return_bps : float
        Current spot return since window start, in bps.
    vol_bps : float
        Volatility estimate in bps (from :func:`yang_zhang_vol`,
        :meth:`TickVolTracker.realized_vol_bps`, etc.).
    remaining_sec : float
        Seconds remaining in the 5-minute window.

    Returns
    -------
    float
        Estimated probability that spot finishes above its window-start
        price, clipped to [0.02, 0.98].
    """
    remaining_min = max(remaining_sec / 60.0, 0.01)

    if vol_bps <= 0.0:
        # Degenerate vol → direction is deterministic
        if spot_return_bps > 0:
            return 0.98
        if spot_return_bps < 0:
            return 0.02
        return 0.50

    denom = vol_bps * math.sqrt(remaining_min)
    z = spot_return_bps / denom
    p = phi(z)
    return max(0.02, min(0.98, p))

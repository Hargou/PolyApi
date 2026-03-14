"""
Spot Momentum Strategy.

Core idea: if BTC spot is up +X bps in the first 2 minutes of a 5-min window,
the "Will BTC go up?" market should reflect that. If the market is underpricing
the move, buy YES. If overpricing, buy NO.

Uses Bayesian update (logistic transform of spot return) and Kelly sizing.
"""

import math
from dataclasses import dataclass

from strategies.base import BaseStrategy, MarketState, Signal


def _logistic(x: float) -> float:
    """Logistic sigmoid: maps any real number to (0, 1)."""
    return 1.0 / (1.0 + math.exp(-x))


def _kelly_fraction(p_hat: float, p_market: float, fraction: float = 0.25) -> float:
    """Fractional Kelly. Returns optimal fraction of bankroll to bet."""
    ev = p_hat - p_market
    if ev <= 0 or p_hat <= 0 or p_hat >= 1:
        return 0.0
    f_star = ev / (p_hat * (1.0 - p_hat))
    return max(0.0, min(1.0, f_star * fraction))


@dataclass
class SpotMomentumConfig:
    """Tunable parameters for the spot momentum strategy."""
    min_edge_bps: float = 300.0     # minimum edge to trade (in bps vs market)
    max_spread_bps: float = 400.0   # skip if spread too wide
    min_elapsed_sec: int = 30       # wait at least 30s for signal to form
    max_elapsed_sec: int = 180      # don't enter after 3 min
    kelly_fraction: float = 0.25    # fractional Kelly (conservative)
    logistic_scale: float = 0.02    # how aggressively spot return maps to probability
    bankroll: float = 10_000.0      # for Kelly sizing
    max_size: int = 200             # max contracts per trade
    slippage_tolerance_bps: int = 200


class SpotMomentumStrategy(BaseStrategy):
    """
    Buy YES when spot momentum suggests price going up and market underprices it.
    Buy NO when spot is dropping and market overprices YES.
    """

    name = "spot_momentum"

    def __init__(self, config: SpotMomentumConfig = None):
        self.config = config or SpotMomentumConfig()

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

        # Bayesian estimate: logistic transform of spot return
        # Positive spot return -> higher P(up) -> higher P(YES)
        p_hat = _logistic(state.spot_return_bps / 10_000 / cfg.logistic_scale)

        # Market price (midpoint as probability)
        p_market = state.midpoint

        # Edge
        ev_bps = (p_hat - p_market) * 10_000

        if abs(ev_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0, f"no edge: ev={ev_bps:.0f} bps",
                          p_hat=p_hat, ev_bps=ev_bps)

        # Direction
        if ev_bps > 0:
            # We think YES is underpriced -> buy YES
            action = "buy_yes"
            price_for_kelly = p_market
        else:
            # We think YES is overpriced -> buy NO (NO price = 1 - YES price)
            action = "buy_no"
            p_hat = 1.0 - p_hat
            p_market = 1.0 - p_market
            price_for_kelly = p_market
            ev_bps = abs(ev_bps)

        # Kelly sizing
        f = _kelly_fraction(p_hat, price_for_kelly, cfg.kelly_fraction)
        size = int(cfg.bankroll * f / price_for_kelly) if price_for_kelly > 0 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly size zero", p_hat=p_hat, ev_bps=ev_bps)

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=f"spot={state.spot_return_bps:.0f}bps, edge={ev_bps:.0f}bps, kelly_f={f:.3f}",
            p_hat=p_hat,
            ev_bps=ev_bps,
        )

    def reset(self):
        pass

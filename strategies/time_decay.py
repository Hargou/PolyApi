"""
Time Decay Strategy.

Uses the Brownian motion framework: as the 5-minute window progresses,
current spot return becomes increasingly predictive of the final outcome
because there's less time for reversals.

P(up) = Phi(return / (sigma * sqrt(remaining)))

Late in the window, even small spot returns create strong directional
conviction. Markets that still show ~50% probability are mispriced.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List

from strategies.base import BaseStrategy, MarketState, Signal


def _phi(x: float) -> float:
    """Standard normal CDF."""
    if x > 8: return 1.0
    if x < -8: return 0.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0: return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class TimeDecayConfig:
    # Vol estimation
    vol_baseline_bps: float = 25.0   # 5-min vol assumption if insufficient data
    vol_min_ticks: int = 5
    vol_floor_bps: float = 3.0       # minimum vol to prevent div by zero

    # Timing: trades in the second half of window when time decay bites
    min_elapsed_sec: int = 90
    max_elapsed_sec: int = 270
    min_remaining_sec: int = 20

    # Signal
    min_spot_move_bps: float = 2.0   # need at least a small directional move
    min_divergence_bps: float = 60.0 # edge vs Polymarket to trade
    max_spread_bps: float = 500.0

    # Sizing
    kelly_fraction: float = 0.30
    bankroll: float = 10_000.0
    max_size: int = 350
    slippage_tolerance_bps: int = 200


class TimeDecayStrategy(BaseStrategy):
    """
    Exploits the mathematical certainty that as remaining time shrinks,
    the current spot direction becomes locked in. Markets that lag this
    reality are mispriced.
    """

    name = "time_decay"

    def __init__(self, config: TimeDecayConfig = None):
        self.config = config or TimeDecayConfig()
        self._spot_history: Dict[str, List[float]] = {}

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "waiting for time decay")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")
        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, "not enough time for fill")
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f}bps")
        if abs(state.spot_return_bps) < cfg.min_spot_move_bps:
            return Signal("hold", 0, 0, f"flat: {state.spot_return_bps:.1f}bps")

        # Track spot for vol estimation
        hist = self._spot_history.setdefault(state.condition_id, [])
        hist.append(state.spot_price)

        # Estimate realized vol
        sigma = self._estimate_vol(hist, cfg)

        # Brownian motion P(up)
        remaining_min = max(state.remaining_sec / 60.0, 0.05)
        z = state.spot_return_bps / (sigma * math.sqrt(remaining_min))
        p_hat = _phi(z)
        p_hat = max(0.02, min(0.98, p_hat))

        # Divergence from market
        p_market = state.midpoint
        divergence_bps = (p_hat - p_market) * 10_000

        if abs(divergence_bps) < cfg.min_divergence_bps:
            return Signal("hold", 0, 0,
                          f"div={divergence_bps:.0f}bp p={p_hat:.3f} sig={sigma:.1f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        # Direction
        if divergence_bps > 0:
            action = "buy_yes"
            entry_p = state.best_ask if state.best_ask > 0 else p_market
            p_kelly = p_hat
        else:
            action = "buy_no"
            entry_p = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            p_kelly = 1.0 - p_hat

        # Fee-aware Kelly
        fee_rate = _eff_fee_rate(entry_p)
        fee_per = entry_p * fee_rate
        eff_cost = entry_p + fee_per
        eff_profit = 1.0 - eff_cost

        if eff_profit <= 0:
            return Signal("hold", 0, 0, "no margin after fees")

        ev = p_kelly * eff_profit - (1 - p_kelly) * eff_cost
        if ev <= 0:
            return Signal("hold", 0, 0, f"-EV after fees")

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))
        size = int(cfg.bankroll * f / entry_p) if entry_p > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly=0")

        rationale = (f"Phi(z={z:.2f})={p_hat:.3f} mkt={p_market:.2f} "
                     f"div={divergence_bps:.0f}bp sig={sigma:.1f} "
                     f"rem={state.remaining_sec}s fee={fee_rate*100:.3f}%")

        return Signal(action=action, size=size,
                      max_slippage_bps=cfg.slippage_tolerance_bps,
                      rationale=rationale, p_hat=p_hat, ev_bps=divergence_bps)

    def _estimate_vol(self, hist: List[float], cfg: TimeDecayConfig) -> float:
        """Estimate intra-window spot volatility from tick data."""
        if len(hist) < cfg.vol_min_ticks:
            return cfg.vol_baseline_bps

        returns = []
        for i in range(1, len(hist)):
            if hist[i - 1] > 0:
                returns.append((hist[i] - hist[i - 1]) / hist[i - 1] * 10_000)

        if len(returns) < 3:
            return cfg.vol_baseline_bps

        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        sigma = math.sqrt(var) if var > 0 else cfg.vol_baseline_bps

        return max(sigma, cfg.vol_floor_bps)

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._spot_history.pop(condition_id, None)

    def reset(self):
        self._spot_history.clear()

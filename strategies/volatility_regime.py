"""
Volatility Regime Strategy.

Estimates intra-window realized volatility from spot price ticks to determine
the trading regime:

- High vol: larger spot returns push prices to extremes where fees are cheap.
  Brownian motion gives strong directional signal. Trade aggressively.
- Low vol: small spot returns keep prices near 50% where fees are ~1.56%.
  Any edge gets eaten by fees. Skip.

This strategy acts as a volatility filter on top of a Brownian motion
directional model, scaling position size by vol regime.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from strategies.base import BaseStrategy, MarketState, Signal


def _phi(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    if x > 8:
        return 1.0
    if x < -8:
        return 0.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class VolatilityRegimeConfig:
    """Tunable parameters for the volatility regime strategy."""
    # Vol estimation
    min_ticks: int = 10                     # need enough observations for vol estimate

    # Vol regime thresholds (on tick-to-tick return std)
    vol_threshold_low: float = 0.0005       # below this = skip, fees eat edge
    vol_threshold_high: float = 0.002       # above this = aggressive sizing

    # Sizing
    base_kelly_fraction: float = 0.15       # normal vol regime
    high_vol_kelly_fraction: float = 0.30   # high vol regime — more aggressive
    bankroll: float = 10_000.0
    max_size: int = 300
    slippage_tolerance_bps: int = 200

    # Timing
    min_elapsed_sec: int = 90               # need enough ticks for vol estimate
    max_elapsed_sec: int = 270              # don't trade too late

    # Edge requirement
    min_edge_bps: float = 60.0              # min divergence from market price (in bps)


class VolatilityRegimeStrategy(BaseStrategy):
    """
    Volatility-regime-aware directional strategy.

    Estimates realized vol from intra-window spot price ticks, then:
    - Skips low-vol windows (fee drag > any realistic edge)
    - Trades normal-vol windows with base Kelly sizing
    - Trades high-vol windows aggressively (fees are cheap at extremes)

    Direction comes from Brownian motion: P(up) = Phi(return / (vol * sqrt(T)))
    """

    name = "volatility_regime"

    def __init__(self, config: VolatilityRegimeConfig = None):
        self.config = config or VolatilityRegimeConfig()
        # Track spot price ticks per condition_id
        self._spot_ticks: Dict[str, List[float]] = {}

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # -- Record spot tick (keep only last 200 to avoid unbounded growth) --
        ticks = self._spot_ticks.setdefault(state.condition_id, [])
        if state.spot_price > 0:
            ticks.append(state.spot_price)
            if len(ticks) > 200:
                self._spot_ticks[state.condition_id] = ticks[-200:]
                ticks = self._spot_ticks[state.condition_id]

        # -- Timing filters --
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early for vol estimate")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late in window")

        # -- Need enough ticks for vol estimation --
        if len(ticks) < cfg.min_ticks:
            return Signal("hold", 0, 0,
                          f"not enough ticks: {len(ticks)}/{cfg.min_ticks}")

        # -- Compute tick-to-tick returns (use last 100 ticks for efficiency) --
        recent = ticks[-100:]
        returns = []
        for i in range(1, len(recent)):
            if recent[i - 1] > 0:
                returns.append((recent[i] - recent[i - 1]) / recent[i - 1])

        if len(returns) < 2:
            return Signal("hold", 0, 0, "not enough returns for vol")

        # -- Realized vol (std of tick-to-tick returns) --
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        realized_vol = math.sqrt(variance) if variance > 0 else 0.0

        # -- Vol regime filter --
        if realized_vol < cfg.vol_threshold_low:
            return Signal("hold", 0, 0,
                          f"low vol regime: vol={realized_vol:.6f} < {cfg.vol_threshold_low}")

        # -- Determine Kelly fraction based on vol regime --
        if realized_vol > cfg.vol_threshold_high:
            kelly_frac = cfg.high_vol_kelly_fraction
            vol_regime = "HIGH"
        else:
            kelly_frac = cfg.base_kelly_fraction
            vol_regime = "NORMAL"

        # -- Brownian motion P(up) --
        # P(up) = Phi(spot_return / (vol * sqrt(remaining_minutes)))
        remaining_min = max(state.remaining_sec / 60.0, 0.01)
        spot_return = state.spot_return_bps / 10_000.0  # convert bps to fraction

        denominator = realized_vol * math.sqrt(remaining_min)
        if denominator <= 0:
            return Signal("hold", 0, 0, "degenerate vol denominator")

        z = spot_return / denominator
        p_hat = _phi(z)
        p_hat = max(0.05, min(0.95, p_hat))

        # -- Divergence from Polymarket midpoint --
        p_market = state.midpoint
        divergence_bps = (p_hat - p_market) * 10_000

        if abs(divergence_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"no edge: div={divergence_bps:.0f}bps p={p_hat:.3f} vol={realized_vol:.6f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        # -- Direction --
        if divergence_bps > 0:
            # YES is underpriced
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            p_hat_kelly = p_hat
            entry_price_kelly = entry_price
        else:
            # YES is overpriced, buy NO
            action = "buy_no"
            entry_price = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            p_hat_kelly = 1.0 - p_hat
            entry_price_kelly = entry_price

        # -- Fee-aware Kelly sizing --
        fee_rate = _eff_fee_rate(entry_price_kelly)
        fee_per_contract = entry_price_kelly * fee_rate
        eff_cost = entry_price_kelly + fee_per_contract
        eff_profit = 1.0 - eff_cost

        if eff_profit <= 0:
            return Signal("hold", 0, 0, "no profit after fees")

        ev = p_hat_kelly * eff_profit - (1.0 - p_hat_kelly) * eff_cost
        if ev <= 0:
            return Signal("hold", 0, 0,
                          f"-EV: {ev:.4f} p={p_hat:.3f} vol={realized_vol:.6f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * kelly_frac))
        size = int(cfg.bankroll * f / entry_price_kelly) if entry_price_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0,
                          f"kelly=0 vol={realized_vol:.6f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        rationale = (f"vol_regime={vol_regime} vol={realized_vol:.6f} "
                     f"z={z:.2f} p={p_hat:.3f} mkt={p_market:.3f} "
                     f"div={divergence_bps:.0f}bp kelly_f={kelly_frac:.2f} "
                     f"fee={fee_rate*100:.3f}% ticks={len(ticks)}")

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=divergence_bps,
        )

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._spot_ticks.pop(condition_id, None)

    def reset(self):
        self._spot_ticks.clear()

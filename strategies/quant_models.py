"""
Quant Models Strategy.

Combines multiple quantitative models on actual crypto price action to predict
5-minute direction, then trades when our prediction diverges from Polymarket's price.

Models:
1. Brownian Motion P(up) -- closed-form from spot return + vol estimate
2. Microprice divergence -- depth-weighted fair value vs naive midpoint
3. Order Book Imbalance -- informed flow direction from bid/ask asymmetry
4. Cross-asset lead-lag -- BTC leads ETH/SOL by seconds
5. Time decay -- returns become more predictive as window closes

All signals are combined into a Bayesian composite P(up), then compared to
Polymarket's price. Fee-aware Kelly sizing. Only trades when divergence is large.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from strategies.base import BaseStrategy, MarketState, Signal


# -- Math primitives --

def _logistic(x: float) -> float:
    if x > 500: return 1.0
    if x < -500: return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _phi(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    if x > 8: return 1.0
    if x < -8: return 0.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0: return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


def _kelly_with_fees(p_hat: float, entry_price: float, fraction: float = 0.25) -> float:
    """Kelly fraction for hold-to-expiry binary bet, accounting for actual fee."""
    fee_per_contract = entry_price * _eff_fee_rate(entry_price)
    eff_cost = entry_price + fee_per_contract
    eff_profit = 1.0 - eff_cost

    if eff_profit <= 0 or p_hat <= 0.01 or p_hat >= 0.99:
        return 0.0

    ev = p_hat * eff_profit - (1 - p_hat) * eff_cost
    if ev <= 0:
        return 0.0

    f_star = ev / eff_profit
    return max(0.0, min(1.0, f_star * fraction))


# -- Config --

@dataclass
class QuantModelsConfig:
    """Tunable parameters for the multi-model quant strategy."""
    # Model weights (sum to ~1.0, but don't need to be exact)
    w_brownian: float = 0.35        # weight on Brownian motion model
    w_microprice: float = 0.15      # weight on microprice divergence
    w_obi: float = 0.15            # weight on order book imbalance
    w_cross_asset: float = 0.15    # weight on cross-asset lead-lag
    w_time_decay: float = 0.20     # weight on time decay model

    # Brownian motion params
    vol_baseline_bps: float = 30.0  # assumed 5-min vol in bps if we can't estimate
    vol_min_ticks: int = 5          # min ticks to estimate vol from data

    # OBI params
    obi_sensitivity: float = 0.10   # how much OBI adjusts probability

    # Cross-asset params
    rho_btc_eth: float = 0.85
    rho_btc_sol: float = 0.75
    rho_eth_sol: float = 0.80
    cross_scale: float = 200.0      # logistic scale for cross signal

    # Time decay
    gamma: float = 2.0              # nonlinearity of time decay effect
    time_scale: float = 150.0       # logistic scale for time-weighted return

    # Trade filters
    min_divergence_bps: float = 80.0  # min edge vs Polymarket (in bps of probability)
    max_spread_bps: float = 500.0
    min_elapsed_sec: int = 20
    max_elapsed_sec: int = 270
    min_remaining_sec: int = 20

    # Sizing
    kelly_fraction: float = 0.25
    bankroll: float = 10_000.0
    max_size: int = 300
    slippage_tolerance_bps: int = 200

    # Fee-aware: prefer trading at extremes
    extreme_boost: float = 1.5      # boost size when price is extreme (< 0.25 or > 0.75)
    extreme_threshold: float = 0.25


class QuantModelsStrategy(BaseStrategy):
    """
    Multi-model quant strategy.

    Combines 5 independent models to estimate P(up) from crypto price action,
    then trades when our composite prediction diverges from Polymarket's price.
    Fee-aware Kelly sizing with preference for extreme prices.
    """

    name = "quant_models"

    def __init__(self, config: QuantModelsConfig = None):
        self.config = config or QuantModelsConfig()
        # Internal state: track spot ticks per window for vol estimation
        self._spot_history: Dict[str, List[float]] = {}  # condition_id -> [prices]

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # -- Filters --
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")
        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, "not enough time")
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f}bps")

        # Track spot prices for vol estimation
        hist = self._spot_history.setdefault(state.condition_id, [])
        hist.append(state.spot_price)

        # -- Model 1: Brownian Motion P(up) --
        p_brownian = self._brownian_model(state, hist)

        # -- Model 2: Microprice divergence --
        p_microprice = self._microprice_model(state)

        # -- Model 3: Order Book Imbalance --
        p_obi = self._obi_model(state)

        # -- Model 4: Cross-Asset Lead-Lag --
        p_cross = self._cross_asset_model(state)

        # -- Model 5: Time Decay --
        p_time = self._time_decay_model(state)

        # -- Combine: weighted average --
        total_w = (cfg.w_brownian + cfg.w_microprice + cfg.w_obi +
                   cfg.w_cross_asset + cfg.w_time_decay)
        p_hat = (
            cfg.w_brownian * p_brownian +
            cfg.w_microprice * p_microprice +
            cfg.w_obi * p_obi +
            cfg.w_cross_asset * p_cross +
            cfg.w_time_decay * p_time
        ) / total_w

        # Clamp
        p_hat = max(0.02, min(0.98, p_hat))

        # -- Divergence from Polymarket --
        p_market = state.midpoint
        divergence_bps = (p_hat - p_market) * 10_000

        if abs(divergence_bps) < cfg.min_divergence_bps:
            return Signal("hold", 0, 0,
                          f"no edge: div={divergence_bps:.0f}bps p_hat={p_hat:.3f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        # -- Direction --
        if divergence_bps > 0:
            # We think YES is underpriced
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
        else:
            # We think NO is underpriced (YES overpriced)
            action = "buy_no"
            entry_price = 1.0 - state.best_bid if state.best_bid > 0 else 1.0 - p_market
            # Flip for NO-side Kelly
            p_hat_for_kelly = 1.0 - p_hat
            entry_price_for_kelly = 1.0 - p_market

        if action == "buy_yes":
            p_hat_for_kelly = p_hat
            entry_price_for_kelly = entry_price

        # -- Fee-aware Kelly sizing --
        f = _kelly_with_fees(p_hat_for_kelly, entry_price_for_kelly, cfg.kelly_fraction)
        size = int(cfg.bankroll * f / entry_price_for_kelly) if entry_price_for_kelly > 0.01 else 0
        size = min(size, cfg.max_size)

        # Boost at extremes (fees are much lower)
        if p_market < cfg.extreme_threshold or p_market > (1 - cfg.extreme_threshold):
            size = min(int(size * cfg.extreme_boost), cfg.max_size)

        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, f"kelly=0 p={p_hat:.3f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        rationale = (f"p={p_hat:.3f} mkt={p_market:.3f} div={divergence_bps:.0f}bp "
                     f"[B={p_brownian:.2f} M={p_microprice:.2f} O={p_obi:.2f} "
                     f"X={p_cross:.2f} T={p_time:.2f}]")

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=divergence_bps,
        )

    def _brownian_model(self, state: MarketState, hist: List[float]) -> float:
        """
        P(up) from Brownian motion: Phi(return / (sigma * sqrt(remaining)))

        Uses realized vol if enough ticks, otherwise baseline assumption.
        """
        cfg = self.config
        remaining = max(state.remaining_sec, 1)

        # Estimate vol from intra-window spot ticks
        if len(hist) >= cfg.vol_min_ticks:
            returns = []
            for i in range(1, len(hist)):
                if hist[i - 1] > 0:
                    returns.append((hist[i] - hist[i - 1]) / hist[i - 1] * 10_000)
            if returns:
                mean_r = sum(returns) / len(returns)
                var = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
                sigma = math.sqrt(var) if var > 0 else cfg.vol_baseline_bps
            else:
                sigma = cfg.vol_baseline_bps
        else:
            sigma = cfg.vol_baseline_bps

        sigma = max(sigma, 1.0)  # floor

        # Normalize: scale sigma by sqrt(remaining) for the remaining window
        z = state.spot_return_bps / (sigma * math.sqrt(remaining / 60.0))
        return _phi(z)

    def _microprice_model(self, state: MarketState) -> float:
        """
        Depth-weighted fair value. Microprice reflects where informed
        participants are placing more size.
        """
        if state.microprice > 0:
            return state.microprice
        return state.midpoint

    def _obi_model(self, state: MarketState) -> float:
        """
        Order Book Imbalance: persistent bid/ask asymmetry reveals
        informed flow direction.
        """
        adjustment = state.obi * self.config.obi_sensitivity
        p = state.midpoint + adjustment
        return max(0.02, min(0.98, p))

    def _cross_asset_model(self, state: MarketState) -> float:
        """
        Cross-asset lead-lag: BTC leads ETH/SOL. If BTC moved but
        this asset's market hasn't repriced, there's a mispricing.
        """
        cfg = self.config
        if not state.other_spot_returns:
            return state.midpoint  # no data, neutral

        # Get correlation weights for this asset
        correlations = {
            "BTC": {"ETH": cfg.rho_btc_eth, "SOL": cfg.rho_btc_sol},
            "ETH": {"BTC": cfg.rho_btc_eth, "SOL": cfg.rho_eth_sol},
            "SOL": {"BTC": cfg.rho_btc_sol, "ETH": cfg.rho_eth_sol},
        }
        asset_corrs = correlations.get(state.asset, {})

        # Weighted sum of other assets' returns
        leader_signal = 0.0
        total_weight = 0.0
        for other_asset, ret in state.other_spot_returns.items():
            rho = asset_corrs.get(other_asset, 0.5)
            leader_signal += rho * ret
            total_weight += rho

        if total_weight == 0:
            return state.midpoint

        leader_signal /= total_weight

        # Map to probability
        return _logistic(leader_signal / cfg.cross_scale)

    def _time_decay_model(self, state: MarketState) -> float:
        """
        As the window progresses, current spot return becomes more predictive.
        p_hat converges to 0 or 1 as remaining time shrinks.
        """
        cfg = self.config
        elapsed_frac = min(state.elapsed_sec / 300.0, 1.0)
        time_factor = elapsed_frac ** cfg.gamma  # nonlinear: accelerates near expiry

        # Time-weighted return signal
        signal = state.spot_return_bps * time_factor
        return _logistic(signal / cfg.time_scale)

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._spot_history.pop(condition_id, None)

    def reset(self):
        self._spot_history.clear()

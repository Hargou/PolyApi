"""
Quant Models V2 Strategy.

Tuned version of quant_models with weights optimized from backtest analysis:
- Brownian weight slashed (0.10 vs 0.35): model was noisy/broken
- Microprice boosted (0.25 vs 0.15): depth-weighted fair value is predictive
- Cross-asset boosted (0.30 vs 0.15): BTC lead-lag is the strongest signal
- Time decay removed (0.00 vs 0.20): redundant with spot momentum
- OBI kept at 0.10 (was 0.15): modest but real signal
- Active weights normalized to sum to 1.0

Other tuning:
- min_divergence_bps: 50 (was 80) — trade more often
- min_active_models: 1 (was 2) — trust strong single models
- kelly_fraction: 0.30 (was 0.25) — more conviction
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from strategies.base import BaseStrategy, MarketState, Signal


# -- Math primitives --

def _logistic(x: float) -> float:
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


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

# Raw weights before normalization
_RAW_W_BROWNIAN = 0.10
_RAW_W_MICROPRICE = 0.25
_RAW_W_OBI = 0.10
_RAW_W_CROSS_ASSET = 0.30
_RAW_W_TIME_DECAY = 0.00  # removed

# Normalize active weights to sum to 1.0
_ACTIVE_SUM = _RAW_W_BROWNIAN + _RAW_W_MICROPRICE + _RAW_W_OBI + _RAW_W_CROSS_ASSET
# _RAW_W_TIME_DECAY is 0 so excluded


@dataclass
class QuantModelsV2Config:
    """Tunable parameters for the multi-model quant strategy v2."""
    # Model weights (normalized to sum to 1.0 among active models)
    w_brownian: float = _RAW_W_BROWNIAN / _ACTIVE_SUM     # ~0.1333
    w_microprice: float = _RAW_W_MICROPRICE / _ACTIVE_SUM  # ~0.3333
    w_obi: float = _RAW_W_OBI / _ACTIVE_SUM               # ~0.1333
    w_cross_asset: float = _RAW_W_CROSS_ASSET / _ACTIVE_SUM  # ~0.4000
    w_time_decay: float = 0.0                               # removed

    # Brownian motion params
    vol_baseline_bps: float = 30.0
    vol_min_ticks: int = 5

    # OBI params
    obi_sensitivity: float = 0.10

    # Cross-asset params
    rho_btc_eth: float = 0.85
    rho_btc_sol: float = 0.75
    rho_eth_sol: float = 0.80
    cross_scale: float = 200.0

    # Time decay (kept for interface but weight is 0)
    gamma: float = 2.0
    time_scale: float = 150.0

    # Trade filters
    min_divergence_bps: float = 50.0   # lowered from 80 — trade more often
    max_spread_bps: float = 500.0
    min_elapsed_sec: int = 20
    max_elapsed_sec: int = 270
    min_remaining_sec: int = 20

    # Sizing
    kelly_fraction: float = 0.30       # raised from 0.25
    bankroll: float = 10_000.0
    max_size: int = 300
    slippage_tolerance_bps: int = 200

    # Fee-aware: prefer trading at extremes
    extreme_boost: float = 1.5
    extreme_threshold: float = 0.25

    # Model gating
    min_active_models: int = 1         # lowered from 2 — trust strong single models


class QuantModelsV2Strategy(BaseStrategy):
    """
    Multi-model quant strategy v2.

    Combines 4 quantitative models (Brownian, microprice, OBI, cross-asset)
    with rebalanced weights. Time decay model removed. Lower divergence
    threshold and higher Kelly fraction for more aggressive trading.
    """

    name = "quant_models_v2"

    def __init__(self, config: QuantModelsV2Config = None):
        self.config = config or QuantModelsV2Config()
        self._spot_history: Dict[str, List[float]] = {}

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
        if state.spot_price > 0:
            hist.append(state.spot_price)
            if len(hist) > 200:
                self._spot_history[state.condition_id] = hist[-200:]
                hist = self._spot_history[state.condition_id]

        # -- Models: each returns (p, weight) or None if no data --
        models = [
            ("B", cfg.w_brownian, self._brownian_model(state, hist)),
            ("M", cfg.w_microprice, self._microprice_model(state)),
            ("O", cfg.w_obi, self._obi_model(state)),
            ("X", cfg.w_cross_asset, self._cross_asset_model(state)),
        ]
        # Time decay model excluded (weight is 0) but kept for reference
        # ("T", cfg.w_time_decay, self._time_decay_model(state)),

        # Only average models that have real signal
        total_w = 0.0
        weighted_sum = 0.0
        model_vals = {}
        for name, weight, p in models:
            if p is not None and weight > 0:
                total_w += weight
                weighted_sum += weight * p
                model_vals[name] = p
            else:
                model_vals[name] = None

        active_count = sum(1 for v in model_vals.values() if v is not None)
        if total_w == 0:
            return Signal("hold", 0, 0, "no active models")

        # Require min_active_models, OR 1 model with very strong conviction
        if active_count < cfg.min_active_models:
            strongest = max((abs(v - 0.5) for v in model_vals.values() if v is not None), default=0)
            if strongest < 0.20:
                return Signal("hold", 0, 0,
                              f"weak single model ({active_count}/4, conv={strongest:.2f})")

        p_hat = weighted_sum / total_w

        # Extract for rationale
        p_brownian = model_vals.get("B")
        p_microprice = model_vals.get("M")
        p_obi = model_vals.get("O")
        p_cross = model_vals.get("X")

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
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
        else:
            action = "buy_no"
            entry_price = 1.0 - state.best_bid if state.best_bid > 0 else 1.0 - p_market
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

        def _fmt(v):
            return f"{v:.2f}" if v is not None else "-"
        rationale = (f"p={p_hat:.3f} mkt={p_market:.3f} div={divergence_bps:.0f}bp "
                     f"[B={_fmt(p_brownian)} M={_fmt(p_microprice)} O={_fmt(p_obi)} "
                     f"X={_fmt(p_cross)}]")

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=divergence_bps,
        )

    def _brownian_model(self, state: MarketState, hist: List[float]) -> Optional[float]:
        """P(up) from Brownian motion: Phi(return / (sigma * sqrt(remaining)))."""
        cfg = self.config
        remaining = max(state.remaining_sec, 1)

        if abs(state.spot_return_bps) < 1.0 and len(hist) < cfg.vol_min_ticks:
            return None

        if len(hist) >= cfg.vol_min_ticks:
            recent = hist[-100:]
            returns = []
            for i in range(1, len(recent)):
                if recent[i - 1] > 0:
                    returns.append((recent[i] - recent[i - 1]) / recent[i - 1] * 10_000)
            if returns:
                mean_r = sum(returns) / len(returns)
                var = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
                sigma = math.sqrt(var) if var > 0 else cfg.vol_baseline_bps
            else:
                sigma = cfg.vol_baseline_bps
        else:
            sigma = cfg.vol_baseline_bps

        sigma = max(sigma, 1.0)
        z = state.spot_return_bps / (sigma * math.sqrt(remaining / 60.0))
        return _phi(z)

    def _microprice_model(self, state: MarketState) -> Optional[float]:
        """Depth-weighted fair value vs naive midpoint."""
        if state.microprice > 0 and state.bid_size_at_best > 0 and state.ask_size_at_best > 0:
            if abs(state.microprice - state.midpoint) > 0.002:
                return state.microprice
        return None

    def _obi_model(self, state: MarketState) -> Optional[float]:
        """Order Book Imbalance direction signal."""
        if abs(state.obi) < 0.03:
            return None
        adjustment = state.obi * self.config.obi_sensitivity
        p = state.midpoint + adjustment
        return max(0.02, min(0.98, p))

    def _cross_asset_model(self, state: MarketState) -> Optional[float]:
        """Cross-asset lead-lag: BTC leads ETH/SOL."""
        cfg = self.config
        if not state.other_spot_returns:
            return None

        correlations = {
            "BTC": {"ETH": cfg.rho_btc_eth, "SOL": cfg.rho_btc_sol},
            "ETH": {"BTC": cfg.rho_btc_eth, "SOL": cfg.rho_eth_sol},
            "SOL": {"BTC": cfg.rho_btc_sol, "ETH": cfg.rho_eth_sol},
        }
        asset_corrs = correlations.get(state.asset, {})

        leader_signal = 0.0
        total_weight = 0.0
        for other_asset, ret in state.other_spot_returns.items():
            rho = asset_corrs.get(other_asset, 0.5)
            leader_signal += rho * ret
            total_weight += rho

        if total_weight == 0:
            return None

        leader_signal /= total_weight

        if abs(leader_signal) < 5.0:
            return None

        return _logistic(leader_signal / cfg.cross_scale)

    def _time_decay_model(self, state: MarketState) -> Optional[float]:
        """Kept for interface compatibility but not used (weight = 0)."""
        cfg = self.config
        elapsed_frac = min(state.elapsed_sec / 300.0, 1.0)
        if elapsed_frac < 0.3:
            return None

        time_factor = elapsed_frac ** cfg.gamma
        signal = state.spot_return_bps * time_factor
        if abs(signal) < 5.0:
            return None

        return _logistic(signal / cfg.time_scale)

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._spot_history.pop(condition_id, None)

    def reset(self):
        self._spot_history.clear()

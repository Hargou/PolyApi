"""
Combo Alpha V4 — Asset-specific FADE thresholds.

Based on: combo_alpha_v2 (combo_alpha_v2.py)
Changes from parent:
  - Asset-specific extreme thresholds instead of universal 0.22/0.78:
    BTC: 0.24/0.76 (less volatile, lower bar for overreaction)
    ETH: 0.22/0.78 (baseline, unchanged)
    SOL: 0.20/0.80 (most volatile, need deeper extremes)
  - Hypothesis: each asset has different overreaction characteristics.
    BTC rarely reaches deep extremes → relaxing catches more BTC fades.
    SOL swings naturally → tightening filters noise.

Investigation: 006 (Asset-specific FADE thresholds)
"""

import math
from dataclasses import dataclass
from typing import Dict, Tuple

from strategies.base import BaseStrategy, MarketState, Signal


def _logistic(x: float) -> float:
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


# Asset-specific extreme thresholds: (low, high)
ASSET_THRESHOLDS: Dict[str, Tuple[float, float]] = {
    "BTC": (0.24, 0.76),   # less volatile → relaxed thresholds
    "ETH": (0.22, 0.78),   # baseline
    "SOL": (0.20, 0.80),   # most volatile → tighter thresholds
}
DEFAULT_THRESHOLDS = (0.22, 0.78)


@dataclass
class ComboAlphaV4Config:
    # Signal weights (sum to 1.0)
    w_spot: float = 0.45
    w_obi: float = 0.30
    w_microprice: float = 0.25

    # Spot momentum params
    logistic_scale: float = 0.04

    # Timing filters
    min_elapsed_sec: int = 15
    max_elapsed_sec: int = 270
    min_remaining_sec: int = 15

    # Spread filter
    max_spread_bps: float = 1000.0

    # Edge threshold
    min_edge_bps: float = 30.0

    # Kelly sizing (asymmetric)
    kelly_confirm: float = 0.40
    kelly_fade: float = 0.20
    bankroll: float = 10_000.0
    max_size: int = 500
    slippage_tolerance_bps: int = 200

    # FADE mode params
    fade_max_elapsed_sec: int = 90
    fade_damping: float = 0.6

    # CONFIRM mode — graduated thresholds
    early_confirm_signals: int = 2
    late_confirm_signals: int = 3
    late_confirm_cutoff_sec: int = 90
    min_composite_strength: float = 0.08
    max_contradiction: float = 0.20

    # p_hat mapping
    signal_to_p_scale: float = 0.25
    p_hat_min: float = 0.05
    p_hat_max: float = 0.95

    # Cross-asset boost
    cross_asset_min_bps: float = 10.0
    cross_asset_weight: float = 0.10


class ComboAlphaV4Strategy(BaseStrategy):
    """
    combo_alpha_v2 with asset-specific extreme thresholds.
    BTC gets relaxed thresholds (0.24/0.76), SOL gets tighter (0.20/0.80).
    """

    name = "combo_alpha_v4"

    def __init__(self, config: ComboAlphaV4Config = None):
        self.config = config or ComboAlphaV4Config()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # -- Timing filters --
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late in window")
        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, "not enough time remaining")

        # -- Asset-specific price extreme gatekeeper --
        extreme_low, extreme_high = ASSET_THRESHOLDS.get(state.asset, DEFAULT_THRESHOLDS)

        p_market = state.midpoint
        is_extreme_low = p_market < extreme_low
        is_extreme_high = p_market > extreme_high
        if not (is_extreme_low or is_extreme_high):
            return Signal("hold", 0, 0,
                          f"price {p_market:.3f} not at {state.asset} extreme "
                          f"({extreme_low}/{extreme_high})")

        # -- Spread filter --
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f}bps too wide")

        # -- Compute signals: each in [-1, +1] --
        # 1. Spot momentum
        spot_raw = _logistic(state.spot_return_bps / 10_000 / cfg.logistic_scale)
        spot_signal = (spot_raw - 0.5) * 2.0

        # 2. Order book imbalance
        obi_signal = max(-1.0, min(1.0, state.obi))

        # 3. Microprice divergence
        microprice_signal = 0.0
        if (state.microprice > 0 and state.midpoint > 0
                and state.bid_size_at_best > 0 and state.ask_size_at_best > 0):
            div = (state.microprice - state.midpoint) / state.midpoint
            microprice_signal = max(-1.0, min(1.0, div * 100.0))

        # -- Weighted composite signal --
        composite = (cfg.w_spot * spot_signal
                     + cfg.w_obi * obi_signal
                     + cfg.w_microprice * microprice_signal)

        # -- Cross-asset boost --
        cross_boost = 0.0
        other_returns = getattr(state, "other_spot_returns", {}) or {}
        if other_returns:
            agreeing = []
            for _, ret_bps in other_returns.items():
                if abs(ret_bps) >= cfg.cross_asset_min_bps:
                    agreeing.append(ret_bps)
            if agreeing:
                avg_cross = sum(agreeing) / len(agreeing)
                if (composite > 0 and avg_cross > 0) or \
                   (composite < 0 and avg_cross < 0):
                    cross_boost = cfg.cross_asset_weight * min(1.0, abs(avg_cross) / 50.0)
                    if composite > 0:
                        composite += cross_boost
                    else:
                        composite -= cross_boost

        # -- Count agreeing signals --
        signals_bullish = sum(1 for s in [spot_signal, obi_signal, microprice_signal] if s > 0.05)
        signals_bearish = sum(1 for s in [spot_signal, obi_signal, microprice_signal] if s < -0.05)

        # -- Contradiction check --
        all_signals = [spot_signal, obi_signal, microprice_signal]

        # -- Determine mode: CONFIRM or FADE --
        is_late = state.elapsed_sec >= cfg.late_confirm_cutoff_sec
        required_confirms = cfg.late_confirm_signals if is_late else cfg.early_confirm_signals

        if composite > cfg.min_composite_strength and signals_bullish >= required_confirms:
            if any(s < -cfg.max_contradiction for s in all_signals):
                mode = None
            else:
                mode = "CONFIRM"
                direction = "bullish"
        elif composite < -cfg.min_composite_strength and signals_bearish >= required_confirms:
            if any(s > cfg.max_contradiction for s in all_signals):
                mode = None
            else:
                mode = "CONFIRM"
                direction = "bearish"
        elif state.elapsed_sec < cfg.fade_max_elapsed_sec:
            mode = "FADE"
            direction = "fade"
        else:
            mode = None

        if mode is None:
            return Signal("hold", 0, 0,
                          f"no signal (comp={composite:.3f}, bull={signals_bullish}, bear={signals_bearish})")

        # -- Compute p_hat --
        if mode == "CONFIRM":
            p_hat = p_market + composite * cfg.signal_to_p_scale
            kelly_frac = cfg.kelly_confirm
        else:
            # FADE: damped mean reversion toward 0.5
            p_fair = 0.5 + cfg.fade_damping * (p_market - 0.5)
            if is_extreme_high and p_fair < p_market:
                p_hat = p_fair
            elif is_extreme_low and p_fair > p_market:
                p_hat = p_fair
            else:
                return Signal("hold", 0, 0, "fade direction unclear")
            kelly_frac = cfg.kelly_fade

        # Clamp p_hat
        p_hat = max(cfg.p_hat_min, min(cfg.p_hat_max, p_hat))

        # -- Edge check --
        ev_bps = (p_hat - p_market) * 10_000
        if abs(ev_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"no edge: ev={ev_bps:.0f}bps ({mode})",
                          p_hat=p_hat, ev_bps=ev_bps)

        # -- Direction --
        if ev_bps > 0:
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
        else:
            action = "buy_no"
            entry_price = 1.0 - state.best_bid if state.best_bid > 0 else 1.0 - p_market

        if action == "buy_yes":
            p_hat_for_kelly = p_hat
            entry_price_for_kelly = entry_price
        else:
            p_hat_for_kelly = 1.0 - p_hat
            entry_price_for_kelly = 1.0 - p_market

        # -- Fee-aware Kelly sizing --
        fee_rate = _eff_fee_rate(entry_price_for_kelly)
        fee_per = entry_price_for_kelly * fee_rate
        eff_cost = entry_price_for_kelly + fee_per
        eff_profit = 1.0 - eff_cost

        if eff_profit <= 0:
            return Signal("hold", 0, 0, "no profit after fees")

        ev = p_hat_for_kelly * eff_profit - (1 - p_hat_for_kelly) * eff_cost
        if ev <= 0:
            return Signal("hold", 0, 0,
                          f"-EV: {ev:.4f} ({mode})",
                          p_hat=p_hat, ev_bps=ev_bps)

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * kelly_frac))
        size = int(cfg.bankroll * f / entry_price_for_kelly) if entry_price_for_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0,
                          f"kelly=0 p={p_hat:.3f} ({mode})",
                          p_hat=p_hat, ev_bps=ev_bps)

        window = "early" if not is_late else "late"
        rationale = (f"{mode}_{window} p={p_hat:.3f} mkt={p_market:.3f} ev={abs(ev_bps):.0f}bp "
                     f"[spot={spot_signal:+.2f} obi={obi_signal:+.2f} "
                     f"mp={microprice_signal:+.2f}] xb={cross_boost:.2f} f={f:.3f} "
                     f"asset={state.asset} thresh={extreme_low}/{extreme_high}")

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=ev_bps,
        )

    def reset(self):
        pass

"""
Combo Alpha Strategy.

Combines fee-extremes gatekeeper with multi-signal confirmation
for Polymarket 5-min binary crypto markets.

Core insight: Polymarket's non-linear fee formula (0.25 * (p*(1-p))^2) drops
to near-zero at price extremes (<0.25 or >0.75), giving ~300+ bps structural edge.
We only trade at extremes, then use 3 independent signals to pick direction.

Dual mode:
  CONFIRM: Price extreme AND 2+ signals agree with direction -> aggressive Kelly
  FADE:    Price extreme early in window, signals neutral -> fade to 0.5

Signals:
  1. Spot momentum (weight 0.45): logistic transform of spot return
  2. Order book imbalance (weight 0.30): bid/ask depth ratio
  3. Microprice divergence (weight 0.25): microprice vs midpoint direction
"""

import math
from dataclasses import dataclass
from typing import Optional

from strategies.base import BaseStrategy, MarketState, Signal


# -- Math primitives --

def _logistic(x: float) -> float:
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _eff_fee_rate(price: float) -> float:
    """Effective fee rate at a given price (Polymarket crypto formula)."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


def _kelly_with_fees(p_hat: float, entry_price: float, fraction: float) -> float:
    """Fee-aware Kelly for hold-to-expiry binary bet."""
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
class ComboAlphaConfig:
    """Tunable parameters for the combo alpha strategy."""
    # Signal weights (sum to 1.0)
    w_spot: float = 0.45
    w_obi: float = 0.30
    w_microprice: float = 0.25

    # Spot momentum params
    logistic_scale: float = 0.04  # less aggressive than old 0.02

    # Price extreme thresholds
    extreme_low: float = 0.22     # price must be below this (match early_fade)
    extreme_high: float = 0.78    # or above this

    # Timing filters
    min_elapsed_sec: int = 15
    max_elapsed_sec: int = 270
    min_remaining_sec: int = 15

    # Spread filter
    max_spread_bps: float = 1000.0  # widened to match Rust risk limits

    # Edge threshold
    min_edge_bps: float = 30.0    # low because fee advantage covers us

    # Kelly sizing
    kelly_confirm: float = 0.40   # aggressive for CONFIRM mode
    kelly_fade: float = 0.20      # conservative for FADE mode
    bankroll: float = 10_000.0
    max_size: int = 500
    slippage_tolerance_bps: int = 200

    # FADE mode params
    fade_max_elapsed_sec: int = 90    # only fade early in window
    fade_damping: float = 0.6         # bias p_hat toward 0.5

    # p_hat mapping
    signal_to_p_scale: float = 0.25   # composite_signal * scale -> p offset from 0.5
    p_hat_min: float = 0.05
    p_hat_max: float = 0.95

    # Minimum signals agreeing for CONFIRM mode
    min_confirm_signals: int = 2


class ComboAlphaStrategy(BaseStrategy):
    """
    Fee-extremes gatekeeper + multi-signal confirmation strategy.

    Only trades when Polymarket price is at extremes (<0.25 or >0.75) where
    fees drop to near-zero. Combines spot momentum, OBI, and microprice
    divergence to pick direction. Dual mode: CONFIRM (signals agree) or
    FADE (signals neutral, fade to 0.5).
    """

    name = "combo_alpha"

    def __init__(self, config: ComboAlphaConfig = None):
        self.config = config or ComboAlphaConfig()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # -- Timing filters --
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late in window")
        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, "not enough time remaining")

        # -- Price extreme gatekeeper --
        p_market = state.midpoint
        is_extreme_low = p_market < cfg.extreme_low
        is_extreme_high = p_market > cfg.extreme_high
        if not (is_extreme_low or is_extreme_high):
            return Signal("hold", 0, 0,
                          f"price {p_market:.3f} not at extreme (<{cfg.extreme_low} or >{cfg.extreme_high})")

        # -- Spread filter --
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f}bps too wide")

        # -- Compute signals: each in [-1, +1] --
        # 1. Spot momentum: logistic transform of spot return
        spot_raw = _logistic(state.spot_return_bps / 10_000 / cfg.logistic_scale)
        spot_signal = (spot_raw - 0.5) * 2.0  # map (0,1) -> (-1,+1)

        # 2. Order book imbalance: already in [-1, +1]
        obi_signal = max(-1.0, min(1.0, state.obi))

        # 3. Microprice divergence: direction signal from microprice vs midpoint
        microprice_signal = 0.0
        microprice_active = False
        if (state.microprice > 0 and state.midpoint > 0
                and state.bid_size_at_best > 0 and state.ask_size_at_best > 0):
            div = (state.microprice - state.midpoint) / state.midpoint
            # Scale: 1% microprice divergence = full signal
            microprice_signal = max(-1.0, min(1.0, div * 100.0))
            microprice_active = abs(div) > 0.001

        # -- Weighted composite signal --
        composite = (cfg.w_spot * spot_signal
                     + cfg.w_obi * obi_signal
                     + cfg.w_microprice * microprice_signal)

        # -- Count agreeing signals --
        # Determine the "extreme direction": if price < 0.25, market says NO is likely
        # If price > 0.75, market says YES is likely
        # A positive signal = bullish (YES direction)
        signals_bullish = sum(1 for s in [spot_signal, obi_signal, microprice_signal] if s > 0.05)
        signals_bearish = sum(1 for s in [spot_signal, obi_signal, microprice_signal] if s < -0.05)

        # -- Determine mode: CONFIRM or FADE --
        if composite > 0.05 and signals_bullish >= cfg.min_confirm_signals:
            mode = "CONFIRM"
            direction = "bullish"
        elif composite < -0.05 and signals_bearish >= cfg.min_confirm_signals:
            mode = "CONFIRM"
            direction = "bearish"
        elif state.elapsed_sec < cfg.fade_max_elapsed_sec:
            # FADE: early window, signals weak/neutral -> fade extreme to 0.5
            mode = "FADE"
            direction = "fade"
        else:
            return Signal("hold", 0, 0,
                          f"no signal (composite={composite:.3f}, bull={signals_bullish}, bear={signals_bearish})")

        # -- Compute p_hat --
        if mode == "CONFIRM":
            p_hat = p_market + composite * cfg.signal_to_p_scale
            kelly_frac = cfg.kelly_confirm
        else:
            # FADE: damped mean reversion toward 0.5
            p_fair = 0.5 + cfg.fade_damping * (p_market - 0.5)
            if is_extreme_high and p_fair < p_market:
                p_hat = p_fair  # buy NO (fade high)
            elif is_extreme_low and p_fair > p_market:
                p_hat = p_fair  # buy YES (fade low)
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
            # Flip for NO-side Kelly
            p_hat_for_kelly = 1.0 - p_hat
            entry_price_for_kelly = 1.0 - p_market

        if action == "buy_yes":
            p_hat_for_kelly = p_hat
            entry_price_for_kelly = entry_price

        # -- Fee-aware Kelly sizing --
        f = _kelly_with_fees(p_hat_for_kelly, entry_price_for_kelly, kelly_frac)
        size = int(cfg.bankroll * f / entry_price_for_kelly) if entry_price_for_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0,
                          f"kelly=0 p={p_hat:.3f} ({mode})",
                          p_hat=p_hat, ev_bps=ev_bps)

        rationale = (f"{mode} p={p_hat:.3f} mkt={p_market:.3f} ev={abs(ev_bps):.0f}bp "
                     f"[spot={spot_signal:+.2f} obi={obi_signal:+.2f} "
                     f"mp={microprice_signal:+.2f}] kelly_f={f:.3f}")

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

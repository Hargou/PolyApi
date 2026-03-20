"""
Early Fade V2 — Bayesian signal + relaxed timing + cross-asset + FADE mode.

Improvements over V1:
  1. Bayesian p_hat (quick_bayesian_p) replaces logistic transform — incorporates
     OBI, microprice, and time-weighted path scaling, not just cumulative spot return.
  2. Timing expanded to match Rust-side risk limits (5-295s) — V1 was self-limiting
     at 20-270s, leaving profitable late-window trades on the table.
  3. Cross-asset momentum confirmation — if BTC leads and ETH/SOL lag, early signal.
  4. Both CONFIRM and FADE modes (V1's +$291 was entirely FADE trades).
"""

import math
from dataclasses import dataclass

from strategies.base import BaseStrategy, MarketState, Signal
from strategies.bayesian import quick_bayesian_p, bayesian_kelly


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class EarlyFadeV2Config:
    # Price thresholds
    upper_threshold: float = 0.78
    lower_threshold: float = 0.22

    # Signal params
    spot_confirm_min_bps: float = 3.0   # lowered from 5.0 — bayesian handles noise

    # Fade mode
    fade_enabled: bool = True
    fade_damping: float = 0.65          # mean reversion dampening
    fade_max_elapsed: int = 90          # only fade early in window

    # Timing (relaxed to match Rust risk limits)
    min_elapsed_sec: int = 10           # was 15
    max_elapsed_sec: int = 295          # was 270 — now matches Rust
    min_remaining_sec: int = 5          # was 20 — now matches Rust

    # Spread filter
    max_spread_bps: float = 1000.0

    # Sizing
    kelly_fraction: float = 0.40        # more aggressive with better signal
    bankroll: float = 10_000.0
    max_size: int = 500
    slippage_tolerance_bps: int = 300   # tolerate more for late-window thin books

    # Edge threshold
    min_edge_bps: float = 30.0          # bayesian is more calibrated

    # Cross-asset boost
    cross_asset_min_bps: float = 10.0
    cross_asset_weight: float = 0.15

    # Bayesian confidence floor
    min_confidence: float = 0.05


class EarlyFadeV2Strategy(BaseStrategy):
    """
    Fee-extremes with Bayesian signal, expanded timing, cross-asset confirmation.
    Both CONFIRM and FADE modes. Only trades at price extremes where fees are near-zero.
    """

    name = "early_fade_v2"

    def __init__(self, config: EarlyFadeV2Config = None):
        self.config = config or EarlyFadeV2Config()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # Timing filters
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")
        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, "not enough time")

        p_market = state.midpoint
        is_high = p_market > cfg.upper_threshold
        is_low = p_market < cfg.lower_threshold

        if not is_high and not is_low:
            return Signal("hold", 0, 0, f"not extreme: {p_market:.2f}")

        # Spread filter
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f}bps")

        # -- Bayesian p_hat --
        p_hat_bayes, confidence = quick_bayesian_p(state)

        # -- Cross-asset boost --
        cross_boost = 0.0
        other_returns = getattr(state, "other_spot_returns", {}) or {}
        if other_returns:
            agreeing_returns = []
            for asset_name, ret_bps in other_returns.items():
                if abs(ret_bps) >= cfg.cross_asset_min_bps:
                    agreeing_returns.append(ret_bps)
            if agreeing_returns:
                avg_cross = sum(agreeing_returns) / len(agreeing_returns)
                if (state.spot_return_bps > 0 and avg_cross > 0) or \
                   (state.spot_return_bps < 0 and avg_cross < 0):
                    cross_boost = cfg.cross_asset_weight * min(1.0, abs(avg_cross) / 50.0)

        # -- Determine direction: CONFIRM or FADE --
        spot_confirms_up = state.spot_return_bps > cfg.spot_confirm_min_bps
        spot_confirms_down = state.spot_return_bps < -cfg.spot_confirm_min_bps

        action = None
        p_hat = None
        mode = None

        if is_high:
            if spot_confirms_up:
                # CONFIRM: spot agrees with high extreme, buy YES
                action = "buy_yes"
                p_hat = max(p_hat_bayes, p_market)
                p_hat = min(0.98, p_hat + cross_boost)
                mode = "confirm_high"
            elif cfg.fade_enabled and state.elapsed_sec < cfg.fade_max_elapsed:
                # FADE: early overreaction, buy cheap NO
                p_fair = 0.5 + cfg.fade_damping * (p_market - 0.5)
                if p_fair < p_market:
                    action = "buy_no"
                    p_hat = 1.0 - p_fair
                    mode = "fade_high"

        elif is_low:
            if spot_confirms_down:
                # CONFIRM: spot agrees with low extreme, buy NO
                action = "buy_no"
                p_hat_no = 1.0 - p_hat_bayes
                p_hat = max(p_hat_no, 1.0 - p_market)
                p_hat = min(0.98, p_hat + cross_boost)
                mode = "confirm_low"
            elif cfg.fade_enabled and state.elapsed_sec < cfg.fade_max_elapsed:
                # FADE: early overreaction, buy cheap YES
                p_fair = 0.5 + cfg.fade_damping * (p_market - 0.5)
                if p_fair > p_market:
                    action = "buy_yes"
                    p_hat = p_fair
                    mode = "fade_low"

        if action is None or p_hat is None:
            return Signal("hold", 0, 0,
                          f"extreme no signal: mkt={p_market:.2f} "
                          f"spot={state.spot_return_bps:.0f}bp")

        # -- Confidence check (only for CONFIRM — FADE uses damping) --
        if mode.startswith("confirm") and confidence < cfg.min_confidence:
            return Signal("hold", 0, 0,
                          f"low confidence: {confidence:.3f}",
                          p_hat=p_hat)

        # -- Edge + Kelly sizing --
        if action == "buy_yes":
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            edge_bps = (p_hat - p_market) * 10_000
            p_hat_kelly = p_hat
            entry_kelly = entry_price
        else:
            entry_price = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            edge_bps = ((1.0 - p_market) - (1.0 - p_hat)) * 10_000
            p_hat_kelly = p_hat  # already flipped for NO
            entry_kelly = entry_price

        if abs(edge_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"edge {edge_bps:.0f}bps < {cfg.min_edge_bps:.0f}",
                          p_hat=p_hat, ev_bps=edge_bps)

        # -- Fee-aware Kelly sizing --
        fee_rate = _eff_fee_rate(entry_kelly)
        fee_per = entry_kelly * fee_rate
        eff_cost = entry_kelly + fee_per
        eff_profit = 1.0 - eff_cost

        if eff_profit <= 0:
            return Signal("hold", 0, 0, "no profit after fees",
                          p_hat=p_hat, ev_bps=edge_bps)

        ev = p_hat_kelly * eff_profit - (1 - p_hat_kelly) * eff_cost
        if ev <= 0:
            return Signal("hold", 0, 0, f"-EV: {ev:.4f}",
                          p_hat=p_hat, ev_bps=edge_bps)

        # Confidence-weighted Kelly for CONFIRM, base for FADE
        if mode.startswith("confirm"):
            f_star = ev / eff_profit
            f = max(0.0, min(1.0, f_star * cfg.kelly_fraction * min(1.0, confidence + 0.5)))
        else:
            f_star = ev / eff_profit
            f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))

        size = int(cfg.bankroll * f / entry_kelly) if entry_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly=0",
                          p_hat=p_hat, ev_bps=edge_bps)

        rationale = (f"{mode} mkt={p_market:.2f} p={p_hat:.3f} "
                     f"edge={edge_bps:.0f}bp fee={fee_rate*100:.3f}% "
                     f"spot={state.spot_return_bps:.0f}bp "
                     f"bayes={p_hat_bayes:.3f} conf={confidence:.2f} "
                     f"xboost={cross_boost:.3f}")

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=edge_bps,
        )

    def reset(self):
        pass

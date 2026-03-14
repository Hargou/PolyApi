"""
Fee-Optimized Extremes Strategy.

Only trades when Polymarket price is at extremes (< 0.22 or > 0.78) where
fees drop from ~1.56% to < 0.4%. Even moderate directional signals become
profitable because the fee drag is near zero.

Two modes:
  - Confirm: price is extreme AND spot confirms direction. High win rate.
  - Fade: price is extreme early in window, likely overreaction. Cheap entry.
"""

import math
from dataclasses import dataclass

from strategies.base import BaseStrategy, MarketState, Signal


def _logistic(x: float) -> float:
    if x > 500: return 1.0
    if x < -500: return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0: return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class FeeExtremesConfig:
    # Price thresholds
    upper_threshold: float = 0.78     # buy YES above this (confirm) or NO (fade)
    lower_threshold: float = 0.22     # buy NO below this (confirm) or YES (fade)

    # Signal params
    logistic_scale: float = 150.0     # scale for spot return -> p_hat
    min_edge_bps: float = 40.0        # can be very low since fees are tiny
    spot_confirm_min_bps: float = 5.0 # min spot return to confirm direction

    # Fade mode
    fade_enabled: bool = True
    fade_damping: float = 0.65        # mean reversion dampening
    fade_max_elapsed: int = 90        # only fade early in window

    # Timing
    min_elapsed_sec: int = 15
    max_elapsed_sec: int = 270
    min_remaining_sec: int = 20

    # Sizing (can be more aggressive since fees are low)
    kelly_fraction: float = 0.35
    bankroll: float = 10_000.0
    max_size: int = 400
    slippage_tolerance_bps: int = 200


class FeeExtremesStrategy(BaseStrategy):
    """
    Only trades at price extremes where fee structure gives us a 4x advantage.
    At midpoint=0.15, fee is 0.41%. At midpoint=0.50, fee is 1.56%.
    """

    name = "fee_extremes"

    def __init__(self, config: FeeExtremesConfig = None):
        self.config = config or FeeExtremesConfig()

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
            return Signal("hold", 0, 0,
                          f"not extreme: {p_market:.2f} in [{cfg.lower_threshold}, {cfg.upper_threshold}]")

        # Spot-implied probability
        p_spot = _logistic(state.spot_return_bps / cfg.logistic_scale)
        spot_confirms_up = state.spot_return_bps > cfg.spot_confirm_min_bps
        spot_confirms_down = state.spot_return_bps < -cfg.spot_confirm_min_bps

        action = None
        p_hat = None
        mode = None

        if is_high:
            # Market thinks YES is likely (price > 0.78)
            if spot_confirms_up:
                # CONFIRM: spot agrees, buy YES. Expensive but high win rate.
                action = "buy_yes"
                p_hat = max(p_spot, p_market)  # take the more bullish estimate
                mode = "confirm_high"
            elif cfg.fade_enabled and state.elapsed_sec < cfg.fade_max_elapsed:
                # FADE: early overreaction, buy cheap NO contracts
                p_fair = 0.5 + cfg.fade_damping * (p_market - 0.5)
                if p_fair < p_market:
                    action = "buy_no"
                    p_hat = 1.0 - p_fair  # our P(NO)
                    mode = "fade_high"
        elif is_low:
            # Market thinks NO is likely (price < 0.22)
            if spot_confirms_down:
                # CONFIRM: spot agrees, buy NO. Expensive but high win rate.
                action = "buy_no"
                p_hat = max(1.0 - p_spot, 1.0 - p_market)
                mode = "confirm_low"
            elif cfg.fade_enabled and state.elapsed_sec < cfg.fade_max_elapsed:
                # FADE: early overreaction, buy cheap YES contracts
                p_fair = 0.5 + cfg.fade_damping * (p_market - 0.5)
                if p_fair > p_market:
                    action = "buy_yes"
                    p_hat = p_fair
                    mode = "fade_low"

        if action is None or p_hat is None:
            return Signal("hold", 0, 0, f"extreme but no signal: mkt={p_market:.2f} spot={state.spot_return_bps:.0f}bp")

        # Compute edge
        if action == "buy_yes":
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            edge_bps = (p_hat - p_market) * 10_000
        else:
            entry_price = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            edge_bps = ((1.0 - p_market) - (1.0 - p_hat)) * 10_000
            # For Kelly on NO side
            p_hat_kelly = p_hat  # already flipped for NO
            entry_price_kelly = entry_price

        if abs(edge_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0, f"edge too small: {edge_bps:.0f}bps",
                          p_hat=p_hat, ev_bps=edge_bps)

        # Kelly sizing with fee
        if action == "buy_yes":
            p_hat_kelly = p_hat
            entry_price_kelly = entry_price

        fee_rate = _eff_fee_rate(entry_price_kelly)
        fee_per = entry_price_kelly * fee_rate
        eff_cost = entry_price_kelly + fee_per
        eff_profit = 1.0 - eff_cost

        if eff_profit <= 0:
            return Signal("hold", 0, 0, "no profit after fees")

        ev = p_hat_kelly * eff_profit - (1 - p_hat_kelly) * eff_cost
        if ev <= 0:
            return Signal("hold", 0, 0, f"-EV: {ev:.4f}")

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))
        size = int(cfg.bankroll * f / entry_price_kelly) if entry_price_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly=0")

        rationale = (f"{mode} mkt={p_market:.2f} p={p_hat:.3f} "
                     f"edge={edge_bps:.0f}bp fee={fee_rate*100:.3f}% spot={state.spot_return_bps:.0f}bp")

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

"""
Liquidity Vacuum Strategy.

When market makers pull liquidity from one side of the order book, they signal
they expect a move in the opposite direction:
  - Thin ask side (bids thicker) → MMs pulled asks → expect price UP
  - Thin bid side (asks thicker) → MMs pulled bids → expect price DOWN

Compute depth_ratio = log(total_bid_depth / total_ask_depth), map through a
logistic to get a probability estimate, confirm with spot return direction,
then trade when the signal diverges from the Polymarket midpoint.

Fee-aware Kelly sizing.  Hold to expiry.
"""

import math
from dataclasses import dataclass
from typing import Dict

from strategies.base import BaseStrategy, MarketState, Signal


# -- Math primitives --

def _logistic(x: float) -> float:
    x = max(-20.0, min(20.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


# -- Config --

@dataclass
class LiquidityVacuumConfig:
    """Tunable parameters for the Liquidity Vacuum strategy."""
    # Depth signal
    depth_ratio_threshold: float = 0.5    # min |log_ratio| to consider trading
    sensitivity: float = 1.5             # logistic scale for depth ratio -> p_hat
    min_total_depth: float = 200.0       # combined bid+ask depth in dollars
    min_levels: int = 2                  # minimum price levels on each side

    # Spot confirmation
    spot_confirm_weight: float = 0.3     # blend weight for spot return confirmation

    # Timing
    min_elapsed_sec: int = 45
    max_elapsed_sec: int = 250

    # Trade filters
    min_edge_bps: float = 70.0           # min divergence from midpoint to trade

    # Sizing
    kelly_fraction: float = 0.20
    bankroll: float = 10_000.0
    max_size: int = 300
    slippage_tolerance_bps: int = 200


class LiquidityVacuumStrategy(BaseStrategy):
    """
    Detect market-maker liquidity withdrawal from one side of the book
    and trade in the direction the MMs are signaling.

    Thick bids + thin asks  →  MMs don't want to sell cheap  →  buy YES
    Thick asks + thin bids  →  MMs don't want to buy dear    →  buy NO
    """

    name = "liquidity_vacuum"

    def __init__(self, config: LiquidityVacuumConfig = None):
        self.config = config or LiquidityVacuumConfig()
        # Track per-condition state if needed in future extensions
        self._state: Dict[str, dict] = {}

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # -- Timing filters --
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early for vacuum signal")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")

        # -- Aggregate depth from raw book levels --
        bid_depth = sum(size for _, size in state.bids)
        ask_depth = sum(size for _, size in state.asks)
        total_depth = bid_depth + ask_depth

        if total_depth < cfg.min_total_depth:
            return Signal("hold", 0, 0,
                          f"thin book: depth=${total_depth:.0f} < ${cfg.min_total_depth:.0f}")

        if len(state.bids) < cfg.min_levels or len(state.asks) < cfg.min_levels:
            return Signal("hold", 0, 0,
                          f"too few levels: bids={len(state.bids)} asks={len(state.asks)}")

        # -- Depth ratio --
        # Guard against zero depth on either side (shouldn't happen after min_levels check)
        if bid_depth <= 0 or ask_depth <= 0:
            return Signal("hold", 0, 0, "zero depth on one side")

        log_ratio = math.log(bid_depth / ask_depth)

        if abs(log_ratio) < cfg.depth_ratio_threshold:
            return Signal("hold", 0, 0,
                          f"symmetric book: |log_ratio|={abs(log_ratio):.2f} < {cfg.depth_ratio_threshold}")

        # -- Base probability from depth asymmetry --
        # Positive log_ratio = bids thicker = MMs pulling asks = expect UP
        p_depth = _logistic(log_ratio * cfg.sensitivity)

        # -- Spot return confirmation --
        # Convert spot_return_bps to a small directional nudge
        # If spot agrees with depth signal, boost confidence; if disagrees, dampen
        spot_direction = 1.0 if state.spot_return_bps > 0 else (-1.0 if state.spot_return_bps < 0 else 0.0)
        depth_direction = 1.0 if log_ratio > 0 else -1.0

        if spot_direction == depth_direction and spot_direction != 0.0:
            # Agreement: boost p_hat away from 0.5
            spot_adjust = cfg.spot_confirm_weight * abs(p_depth - 0.5)
            if p_depth > 0.5:
                p_hat = p_depth + spot_adjust
            else:
                p_hat = p_depth - spot_adjust
        elif spot_direction != 0.0 and spot_direction != depth_direction:
            # Disagreement: dampen p_hat toward 0.5
            spot_adjust = cfg.spot_confirm_weight * abs(p_depth - 0.5) * 0.5
            if p_depth > 0.5:
                p_hat = p_depth - spot_adjust
            else:
                p_hat = p_depth + spot_adjust
        else:
            # Spot is flat, use depth signal alone
            p_hat = p_depth

        p_hat = max(0.02, min(0.98, p_hat))

        # -- Divergence from Polymarket midpoint --
        p_market = state.midpoint
        divergence_bps = (p_hat - p_market) * 10_000

        if abs(divergence_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"no edge: div={divergence_bps:.0f}bp p={p_hat:.3f} ratio={log_ratio:.2f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        # -- Direction --
        if divergence_bps > 0:
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            p_kelly = p_hat
        else:
            action = "buy_no"
            entry_price = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            p_kelly = 1.0 - p_hat

        # -- Fee-aware Kelly sizing --
        fee_rate = _eff_fee_rate(entry_price)
        fee_per = entry_price * fee_rate
        eff_cost = entry_price + fee_per
        eff_profit = 1.0 - eff_cost

        if eff_profit <= 0:
            return Signal("hold", 0, 0, "no margin after fees")

        ev = p_kelly * eff_profit - (1.0 - p_kelly) * eff_cost
        if ev <= 0:
            return Signal("hold", 0, 0,
                          f"-EV: {ev:.4f} p={p_hat:.3f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))
        size = int(cfg.bankroll * f / entry_price) if entry_price > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly=0")

        rationale = (
            f"vacuum: log_ratio={log_ratio:.2f} p={p_hat:.3f} mkt={p_market:.2f} "
            f"div={divergence_bps:.0f}bp bid$={bid_depth:.0f} ask$={ask_depth:.0f} "
            f"spot={state.spot_return_bps:.0f}bp fee={fee_rate*100:.3f}%"
        )

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=divergence_bps,
        )

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._state.pop(condition_id, None)

    def reset(self):
        self._state.clear()

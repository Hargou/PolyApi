"""
Binary Reversal Strategy — Vol-weighted early fade.

Extends early_fade's core insight (extremes revert in first 80s) with volatility
conditioning. The key addition: don't fade in CALM (noise, random) or STORM
(legitimate move) regimes. Only fade in NORMAL vol where overreaction is most
likely — thin liquidity + moderate moves = price overshoot.

Uses Yang-Zhang vol from vol_utils.py for regime classification.
"""

import math
from dataclasses import dataclass

from strategies.base import BaseStrategy, MarketState, Signal


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class BinaryReversalConfig:
    # Price thresholds
    upper_threshold: float = 0.78
    lower_threshold: float = 0.22

    # Timing (tighter than early_fade — highest conviction window)
    min_elapsed_sec: int = 10
    max_elapsed_sec: int = 80       # tighter than early_fade's 90
    min_remaining_sec: int = 5

    # Volatility regime (bps)
    baseline_vol_bps: float = 40.0  # 5-min crypto baseline
    calm_ratio: float = 0.5        # vol < 0.5x baseline = skip (too random)
    storm_ratio: float = 2.5       # vol > 2.5x baseline = skip (justified move)

    # Fade damping — inversely scaled by vol ratio
    base_damping: float = 0.65     # same as early_fade at vol_ratio=1.0

    # Spread filter
    max_spread_bps: float = 1000.0

    # Spot confirmation gate (skip fade when spot agrees with extreme)
    spot_confirm_min_bps: float = 5.0

    # Sizing
    kelly_fraction: float = 0.40
    bankroll: float = 10_000.0
    max_size: int = 500
    slippage_tolerance_bps: int = 300
    min_edge_bps: float = 40.0   # raised from 30 to filter marginal trades


class BinaryReversalStrategy(BaseStrategy):
    """
    Vol-weighted early fade. Only fades NORMAL-vol extremes.
    CALM = skip (noise). STORM = skip (justified). NORMAL = fade overreaction.
    """

    name = "binary_reversal"

    def __init__(self, config: BinaryReversalConfig = None):
        self.config = config or BinaryReversalConfig()
        self._tick_prices = {}  # condition_id -> list of prices for vol estimation

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # Timing gate
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late for fade")
        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, "not enough time")

        # Price extreme gate
        p_market = state.midpoint
        is_high = p_market > cfg.upper_threshold
        is_low = p_market < cfg.lower_threshold

        if not is_high and not is_low:
            return Signal("hold", 0, 0, f"not extreme: {p_market:.2f}")

        # Spread filter
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0, f"spread {state.spread_bps:.0f}bps")

        # Spot confirmation gate: if spot agrees with extreme, it's a real move, don't fade
        if is_high and state.spot_return_bps > cfg.spot_confirm_min_bps:
            return Signal("hold", 0, 0,
                          f"spot confirms high: {state.spot_return_bps:.0f}bps")
        if is_low and state.spot_return_bps < -cfg.spot_confirm_min_bps:
            return Signal("hold", 0, 0,
                          f"spot confirms low: {state.spot_return_bps:.0f}bps")

        # -- Volatility estimation from spot ticks --
        hist = self._tick_prices.setdefault(state.condition_id, [])
        if state.spot_price > 0:
            hist.append(state.spot_price)
            if len(hist) > 200:
                self._tick_prices[state.condition_id] = hist[-200:]
                hist = self._tick_prices[state.condition_id]

        vol_bps = self._estimate_vol(hist)

        # Regime classification
        if vol_bps <= 0:
            # Not enough data — use early_fade's unconditional approach
            vol_ratio = 1.0
            regime = "UNKNOWN"
        else:
            vol_ratio = vol_bps / cfg.baseline_vol_bps
            if vol_ratio < cfg.calm_ratio:
                return Signal("hold", 0, 0,
                              f"CALM regime vol={vol_bps:.1f}bps (skip)")
            if vol_ratio > cfg.storm_ratio:
                return Signal("hold", 0, 0,
                              f"STORM regime vol={vol_bps:.1f}bps (justified)")
            regime = "NORMAL"

        # -- Fixed damping (vol-adjusted was worse than fixed in backtest) --
        # Vol regime acts as a FILTER only — don't adjust the fade signal
        damping = cfg.base_damping

        # Mean reversion: fair value is between extreme and 0.5
        p_fair = 0.5 + damping * (p_market - 0.5)

        action = None
        p_hat = None

        if is_high and p_fair < p_market:
            # Price too high, fade to NO
            action = "buy_no"
            p_hat = 1.0 - p_fair  # P(NO wins)
        elif is_low and p_fair > p_market:
            # Price too low, fade to YES
            action = "buy_yes"
            p_hat = p_fair

        if action is None or p_hat is None:
            return Signal("hold", 0, 0, "no fade signal")

        # -- Edge check --
        if action == "buy_yes":
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            edge_bps = (p_hat - p_market) * 10_000
            p_kelly = p_hat
            entry_kelly = entry_price
        else:
            entry_price = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            edge_bps = ((1.0 - p_market) - (1.0 - p_hat)) * 10_000
            p_kelly = p_hat
            entry_kelly = entry_price

        if abs(edge_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"edge {edge_bps:.0f}bps < {cfg.min_edge_bps:.0f}")

        # -- Fee-aware Kelly sizing --
        fee_rate = _eff_fee_rate(entry_kelly)
        fee_per = entry_kelly * fee_rate
        eff_cost = entry_kelly + fee_per
        eff_profit = 1.0 - eff_cost

        if eff_profit <= 0:
            return Signal("hold", 0, 0, "no profit after fees")

        ev = p_kelly * eff_profit - (1 - p_kelly) * eff_cost
        if ev <= 0:
            return Signal("hold", 0, 0, f"-EV: {ev:.4f}")

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))
        size = int(cfg.bankroll * f / entry_kelly) if entry_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly=0")

        rationale = (f"fade_{regime.lower()} mkt={p_market:.2f} p_fair={p_fair:.3f} "
                     f"p_hat={p_hat:.3f} edge={edge_bps:.0f}bp "
                     f"vol={vol_bps:.1f}bps ratio={vol_ratio:.2f} "
                     f"damp={damping:.3f}")

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=edge_bps,
        )

    def _estimate_vol(self, prices):
        """Simple tick-to-tick vol in bps. Falls back to 0 if not enough data."""
        if len(prices) < 4:
            return 0.0
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1] * 10_000)
        if len(returns) < 3:
            return 0.0
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        return var ** 0.5 if var > 0 else 0.0

    def on_market_resolved(self, condition_id, outcome, pnl):
        self._tick_prices.pop(condition_id, None)

    def reset(self):
        self._tick_prices.clear()

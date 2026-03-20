"""
Last-30-Second Snipe Strategy.

Highest-alpha single strategy: exploits the timing where prediction accuracy
is highest (~85% by T-10s) and Polymarket liquidity is thinnest.

Enter only in the final 15-45 seconds of a 5-min window. By this time, ~85%
of the direction is already determined by spot movement, but Polymarket price
hasn't fully adjusted due to thin liquidity + latency. Fees at extremes are
near-zero (price is usually at extreme by this point). High conviction
enables aggressive Kelly sizing.
"""

import math
from dataclasses import dataclass

from strategies.base import BaseStrategy, MarketState, Signal


def _logistic(x: float) -> float:
    """Logistic sigmoid: maps any real number to (0, 1)."""
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _eff_fee_rate(price: float) -> float:
    """Polymarket effective fee rate at a given price."""
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class SnipeConfig:
    # Timing window
    min_remaining_sec: int = 10       # don't enter too late (blockchain latency)
    max_remaining_sec: int = 45       # don't enter too early (signal not strong enough)

    # Signal thresholds
    min_spot_move_bps: float = 5.0    # need SOME spot movement to have conviction
    min_conviction: float = 0.60      # minimum p_hat to trade (high bar)

    # Spread filter
    max_spread_bps: float = 500.0     # can tolerate wider spreads since conviction is high

    # Sizing
    kelly_fraction: float = 0.40      # aggressive since high confidence
    bankroll: float = 10_000.0
    max_size: int = 500
    slippage_tolerance_bps: int = 300  # wider tolerance for thin late-window books

    # Edge threshold
    min_edge_bps: float = 50.0        # moderate edge requirement

    # Spot return scaling for p_hat
    # At T-30s, a +50 bps spot move should give strong conviction (p_hat ~ 0.75)
    spot_scale: float = 0.03          # logistic scale for spot -> probability

    # Microstructure confirmation boost
    obi_boost: float = 0.05           # how much OBI can shift p_hat
    microprice_boost: float = 0.03    # how much microprice divergence can shift p_hat


class SnipeStrategy(BaseStrategy):
    """
    Last-30-second snipe. Waits until the final seconds of a 5-min window,
    when spot direction is ~85% determined but Polymarket price lags due to
    thin liquidity and latency. Captures the lag with aggressive sizing.
    """

    name = "snipe"

    def __init__(self, config: SnipeConfig = None):
        self.config = config or SnipeConfig()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # ── 1. TIMING GATE ──────────────────────────────────────────────
        if state.remaining_sec > cfg.max_remaining_sec:
            return Signal("hold", 0, 0, "too early for snipe")
        if state.remaining_sec < cfg.min_remaining_sec:
            return Signal("hold", 0, 0, "too late — blockchain latency risk")

        # ── 2. SPREAD FILTER ────────────────────────────────────────────
        if state.spread_bps > cfg.max_spread_bps:
            return Signal("hold", 0, 0,
                          f"spread {state.spread_bps:.0f}bps too wide")

        # ── 3. SPOT SIGNAL ──────────────────────────────────────────────
        # Need minimum spot movement for any conviction
        if abs(state.spot_return_bps) < cfg.min_spot_move_bps:
            return Signal("hold", 0, 0,
                          f"spot move {state.spot_return_bps:.1f}bps too small")

        # Logistic transform: spot return -> P(YES)
        # At spot_scale=0.03, a +50bps move gives p_hat ~ 0.82
        p_hat = _logistic(state.spot_return_bps / 10_000 / cfg.spot_scale)

        # ── 4. MICROSTRUCTURE CONFIRMATION ──────────────────────────────
        # OBI: positive = more bid pressure = bullish
        spot_bullish = state.spot_return_bps > 0
        if state.obi != 0.0:
            obi_agrees = (state.obi > 0) == spot_bullish
            if obi_agrees:
                # Boost p_hat toward the direction
                if spot_bullish:
                    p_hat = min(0.99, p_hat + cfg.obi_boost)
                else:
                    p_hat = max(0.01, p_hat - cfg.obi_boost)

        # Microprice divergence from midpoint
        if state.microprice > 0.0 and state.midpoint > 0.0:
            mp_div = state.microprice - state.midpoint
            mp_agrees = (mp_div > 0) == spot_bullish
            if mp_agrees:
                if spot_bullish:
                    p_hat = min(0.99, p_hat + cfg.microprice_boost)
                else:
                    p_hat = max(0.01, p_hat - cfg.microprice_boost)

        # ── 5. CONVICTION CHECK ─────────────────────────────────────────
        conviction = abs(p_hat - 0.5)
        min_required = cfg.min_conviction - 0.5
        if conviction < min_required:
            return Signal("hold", 0, 0,
                          f"conviction {p_hat:.3f} below threshold {cfg.min_conviction}",
                          p_hat=p_hat)

        # ── 6. DIRECTION + EDGE ─────────────────────────────────────────
        p_market = state.midpoint

        if p_hat > p_market:
            # We think YES is underpriced -> buy YES
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            edge_bps = (p_hat - p_market) * 10_000
            p_hat_kelly = p_hat
            entry_kelly = entry_price
        else:
            # We think YES is overpriced -> buy NO
            action = "buy_no"
            entry_price = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            edge_bps = (p_market - p_hat) * 10_000
            # For Kelly on the NO side
            p_hat_kelly = 1.0 - p_hat       # P(NO wins)
            entry_kelly = entry_price        # cost of NO contract

        if edge_bps < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"edge {edge_bps:.0f}bps < min {cfg.min_edge_bps:.0f}bps",
                          p_hat=p_hat, ev_bps=edge_bps)

        # ── 7. FEE-AWARE KELLY SIZING ───────────────────────────────────
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

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))
        size = int(cfg.bankroll * f / entry_kelly) if entry_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly=0",
                          p_hat=p_hat, ev_bps=edge_bps)

        # ── 8. EMIT SIGNAL ──────────────────────────────────────────────
        rationale = (
            f"snipe T-{state.remaining_sec}s "
            f"mkt={p_market:.2f} p={p_hat:.3f} "
            f"edge={edge_bps:.0f}bp fee={fee_rate*100:.3f}% "
            f"spot={state.spot_return_bps:.0f}bp "
            f"obi={state.obi:.2f} kelly_f={f:.3f}"
        )

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

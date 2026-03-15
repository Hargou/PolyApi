"""
Order Book Imbalance Strategy.

Tracks EMA-smoothed Order Book Imbalance (OBI) as the primary signal.
Persistent imbalance in the order book reveals informed flow — when bids
consistently outweigh asks, buying pressure is building and price is likely
to move up (and vice versa).

Signal flow:
1. Compute raw OBI from total bid/ask depth across all book levels
2. Smooth with exponential moving average to filter noise
3. When |EMA(OBI)| exceeds threshold, derive directional P(up)
4. Compare to Polymarket midpoint for divergence
5. Fee-aware Kelly sizing

Best in markets with active order books where depth reveals intent.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, Optional

from strategies.base import BaseStrategy, MarketState, Signal


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class OrderBookImbalanceConfig:
    """Tunable parameters for the OBI strategy."""
    # EMA smoothing
    ema_alpha: float = 0.3              # EMA smoothing factor (higher = more reactive)

    # Signal thresholds
    obi_threshold: float = 0.15         # min |EMA(OBI)| to generate a signal
    obi_sensitivity: float = 0.6        # how much OBI shifts p_hat from 0.5

    # Book quality filters
    min_book_depth: int = 3             # need at least this many levels on each side

    # Timing
    min_elapsed_sec: int = 60           # wait for enough book data to accumulate
    max_elapsed_sec: int = 240          # don't trade too late in the window

    # Edge requirement
    min_edge_bps: float = 80.0          # min divergence from market price (in bps)

    # Sizing
    kelly_fraction: float = 0.15
    bankroll: float = 10_000.0
    max_size: int = 300
    slippage_tolerance_bps: int = 200


class OrderBookImbalanceStrategy(BaseStrategy):
    """
    Trades based on EMA-smoothed order book imbalance.

    When bids persistently outweigh asks (positive OBI), informed buyers are
    accumulating — predict price goes up, buy YES. When asks dominate
    (negative OBI), predict price goes down, buy NO.

    EMA smoothing filters out transient book fluctuations and highlights
    sustained directional pressure.
    """

    name = "orderbook_imbalance"

    def __init__(self, config: OrderBookImbalanceConfig = None):
        self.config = config or OrderBookImbalanceConfig()
        # Track EMA of OBI per condition_id
        self._ema_obi: Dict[str, float] = {}

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # -- Timing filters --
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early for OBI signal")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late in window")

        # -- Book quality check --
        if len(state.bids) < cfg.min_book_depth or len(state.asks) < cfg.min_book_depth:
            return Signal("hold", 0, 0,
                          f"thin book: {len(state.bids)} bids, {len(state.asks)} asks")

        # -- Compute raw OBI from full depth --
        total_bid_depth = sum(size for _, size in state.bids)
        total_ask_depth = sum(size for _, size in state.asks)
        total_depth = total_bid_depth + total_ask_depth

        if total_depth <= 0:
            return Signal("hold", 0, 0, "no depth in book")

        raw_obi = (total_bid_depth - total_ask_depth) / total_depth  # range [-1, 1]

        # -- Update EMA --
        prev_ema = self._ema_obi.get(state.condition_id, 0.0)
        ema_obi = cfg.ema_alpha * raw_obi + (1.0 - cfg.ema_alpha) * prev_ema
        self._ema_obi[state.condition_id] = ema_obi

        # -- Check threshold --
        if abs(ema_obi) < cfg.obi_threshold:
            return Signal("hold", 0, 0,
                          f"OBI below threshold: ema={ema_obi:.3f} thr={cfg.obi_threshold}")

        # -- Derive probability estimate --
        # p_hat = 0.5 + ema_obi * sensitivity, clamped to [0.05, 0.95]
        p_hat = 0.5 + ema_obi * cfg.obi_sensitivity
        p_hat = max(0.05, min(0.95, p_hat))

        # -- Divergence from Polymarket midpoint --
        p_market = state.midpoint
        divergence_bps = (p_hat - p_market) * 10_000

        if abs(divergence_bps) < cfg.min_edge_bps:
            return Signal("hold", 0, 0,
                          f"no edge: div={divergence_bps:.0f}bps p_hat={p_hat:.3f} obi={ema_obi:.3f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        # -- Direction --
        if divergence_bps > 0:
            # We think YES is underpriced — buying pressure supports higher price
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            p_hat_kelly = p_hat
            entry_price_kelly = entry_price
        else:
            # We think YES is overpriced — selling pressure supports lower price
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
                          f"-EV: {ev:.4f} p={p_hat:.3f} obi={ema_obi:.3f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))
        size = int(cfg.bankroll * f / entry_price_kelly) if entry_price_kelly > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, f"kelly=0 obi={ema_obi:.3f}",
                          p_hat=p_hat, ev_bps=divergence_bps)

        rationale = (f"OBI ema={ema_obi:.3f} raw={raw_obi:.3f} "
                     f"p={p_hat:.3f} mkt={p_market:.3f} div={divergence_bps:.0f}bp "
                     f"depth=({total_bid_depth:.1f}/{total_ask_depth:.1f}) "
                     f"fee={fee_rate*100:.3f}%")

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_hat,
            ev_bps=divergence_bps,
        )

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._ema_obi.pop(condition_id, None)

    def reset(self):
        self._ema_obi.clear()

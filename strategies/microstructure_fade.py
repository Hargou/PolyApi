"""
Microstructure Fade — OBI confirms early extreme reversals.

When price hits an extreme (< 0.22 or > 0.78) in the first 60 seconds AND
the order book is imbalanced AGAINST the extreme (more volume on the reversion
side), it confirms the overreaction. This is the highest-conviction fade signal.

Example: price at 0.82 (extreme high) + ask_depth >> bid_depth (sellers piling in)
→ market likely overreacted → buy NO cheap.
"""

from dataclasses import dataclass

from strategies.base import BaseStrategy, MarketState, Signal


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


@dataclass
class MicrostructureFadeConfig:
    # Price thresholds
    upper_threshold: float = 0.78
    lower_threshold: float = 0.22

    # Timing (early window for freshest order flow)
    min_elapsed_sec: int = 10
    max_elapsed_sec: int = 60       # tighter than early_fade — first 60s only
    min_remaining_sec: int = 5

    # OBI confirmation thresholds
    # OBI must point OPPOSITE to extreme to confirm fade
    min_obi_magnitude: float = 0.08  # needs real L2 book data (synthetic OBI ~0.025 is noise)

    # Spread filter
    max_spread_bps: float = 1000.0

    # Sizing
    kelly_fraction: float = 0.35
    bankroll: float = 10_000.0
    max_size: int = 500
    slippage_tolerance_bps: int = 300
    min_edge_bps: float = 30.0

    # Fixed p_hat for OBI-confirmed fades (high conviction)
    fade_p_hat: float = 0.65


class MicrostructureFadeStrategy(BaseStrategy):
    """
    Fades early-window extremes when order book imbalance confirms reversal.
    Only trades when OBI is opposite to the extreme direction.
    """

    name = "microstructure_fade"

    def __init__(self, config: MicrostructureFadeConfig = None):
        self.config = config or MicrostructureFadeConfig()

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # Timing gate
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "too early")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "past microstructure window")
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

        # -- OBI confirmation --
        # Use raw depth from state (bid_depth, ask_depth are in dollars)
        total_depth = state.bid_depth + state.ask_depth
        if total_depth <= 0:
            return Signal("hold", 0, 0, "no book depth")

        obi = state.obi  # already computed: (bid - ask) / (bid + ask), [-1, +1]

        # Check if OBI points OPPOSITE to extreme (confirming fade)
        action = None
        obi_confirms = False

        if is_high and obi < -cfg.min_obi_magnitude:
            # Price extreme HIGH + negative OBI (more asks) = sellers confirm overreaction
            action = "buy_no"
            obi_confirms = True
        elif is_low and obi > cfg.min_obi_magnitude:
            # Price extreme LOW + positive OBI (more bids) = buyers confirm overreaction
            action = "buy_yes"
            obi_confirms = True

        if not obi_confirms:
            return Signal("hold", 0, 0,
                          f"OBI doesn't confirm fade: obi={obi:.3f} "
                          f"(need {'<-' if is_high else '>'}{cfg.min_obi_magnitude})")

        # -- Sizing --
        p_hat = cfg.fade_p_hat  # fixed high-conviction estimate

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

        # Fee-aware Kelly
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

        rationale = (f"micro_fade mkt={p_market:.2f} obi={obi:.3f} "
                     f"p_hat={p_hat:.3f} edge={edge_bps:.0f}bp "
                     f"depth=${total_depth:.0f}")

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

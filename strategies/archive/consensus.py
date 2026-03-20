"""
Consensus Strategy.

Only trades when MULTIPLE independent signals agree on direction.  This
dramatically increases win rate at the cost of fewer trades -- a high-conviction
filter.

Five internal signals are computed on every tick:
  1. Spot direction        (spot_return_bps)
  2. Order Book Imbalance  (state.obi)
  3. Microprice divergence (state.microprice vs state.midpoint)
  4. Brownian P(up)        (time-weighted Phi from spot return + remaining time)
  5. Book depth asymmetry  (log ratio of bid/ask depth)

Each signal votes +1 (YES), -1 (NO), or 0 (neutral / insufficient data).
A trade is generated only when at least ``min_signals_agree`` votes point in
the same direction.  Because of this high bar, we can use a lower edge
threshold and higher Kelly fraction.

Fee-aware Kelly sizing.  Hold to expiry.
"""

import math
from dataclasses import dataclass
from typing import Dict, List

from strategies.base import BaseStrategy, MarketState, Signal


# -- Math primitives --

def _phi(x: float) -> float:
    """Standard normal CDF approximation."""
    if x > 8:
        return 1.0
    if x < -8:
        return 0.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _logistic(x: float) -> float:
    x = max(-20.0, min(20.0, x))
    return 1.0 / (1.0 + math.exp(-x))


def _eff_fee_rate(price: float) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return 0.25 * (price * (1.0 - price)) ** 2


# -- Config --

@dataclass
class ConsensusConfig:
    """Tunable parameters for the Consensus strategy."""
    # Voting
    min_signals_agree: int = 4       # out of 5 signals must agree

    # Dead zones (signal must exceed these to count as a vote)
    spot_dead_zone: float = 0.0001   # spot return threshold (fractional, not bps)
    obi_dead_zone: float = 0.05      # OBI threshold
    microprice_dead_zone: float = 0.005  # microprice - midpoint threshold
    depth_dead_zone: float = 0.1     # min |log(bid_depth/ask_depth)|

    # Brownian model
    brownian_sigma: float = 0.001    # assumed 5-min fractional vol
    brownian_yes_threshold: float = 0.55
    brownian_no_threshold: float = 0.45

    # Timing
    min_elapsed_sec: int = 90
    max_elapsed_sec: int = 250

    # Trade filters
    min_edge_bps: float = 50.0       # lower than normal because consensus is strong

    # Sizing (aggressive because selection is strict)
    kelly_fraction: float = 0.25
    bankroll: float = 10_000.0
    max_size: int = 350
    slippage_tolerance_bps: int = 200


class ConsensusStrategy(BaseStrategy):
    """
    High-conviction voting strategy.  Aggregates five independent directional
    signals and only trades when a super-majority agree.  Fewer trades, higher
    expected win rate.
    """

    name = "consensus"

    def __init__(self, config: ConsensusConfig = None):
        self.config = config or ConsensusConfig()
        # Per-condition tracking for vol estimation
        self._spot_history: Dict[str, List[float]] = {}

    def evaluate(self, state: MarketState) -> Signal:
        cfg = self.config

        # -- Timing filters --
        if state.elapsed_sec < cfg.min_elapsed_sec:
            return Signal("hold", 0, 0, "waiting for consensus data")
        if state.elapsed_sec > cfg.max_elapsed_sec:
            return Signal("hold", 0, 0, "too late")

        # Track spot prices (capped to prevent unbounded growth)
        hist = self._spot_history.setdefault(state.condition_id, [])
        if state.spot_price > 0:
            hist.append(state.spot_price)
            if len(hist) > 100:
                self._spot_history[state.condition_id] = hist[-100:]

        # -- Compute each signal vote --
        votes: Dict[str, int] = {}
        details: Dict[str, str] = {}

        # 1. Spot direction
        # Convert spot_return_bps to fractional return for threshold comparison
        spot_return_frac = state.spot_return_bps / 10_000.0
        if spot_return_frac > cfg.spot_dead_zone:
            votes["spot"] = 1
            details["spot"] = f"+1(ret={state.spot_return_bps:.1f}bp)"
        elif spot_return_frac < -cfg.spot_dead_zone:
            votes["spot"] = -1
            details["spot"] = f"-1(ret={state.spot_return_bps:.1f}bp)"
        else:
            votes["spot"] = 0
            details["spot"] = f"0(flat)"

        # 2. Order Book Imbalance
        if state.obi > cfg.obi_dead_zone:
            votes["obi"] = 1
            details["obi"] = f"+1(obi={state.obi:.3f})"
        elif state.obi < -cfg.obi_dead_zone:
            votes["obi"] = -1
            details["obi"] = f"-1(obi={state.obi:.3f})"
        else:
            votes["obi"] = 0
            details["obi"] = f"0(obi={state.obi:.3f})"

        # 3. Microprice vs midpoint
        microprice_diff = state.microprice - state.midpoint
        if microprice_diff > cfg.microprice_dead_zone:
            votes["microprice"] = 1
            details["microprice"] = f"+1(diff={microprice_diff:.4f})"
        elif microprice_diff < -cfg.microprice_dead_zone:
            votes["microprice"] = -1
            details["microprice"] = f"-1(diff={microprice_diff:.4f})"
        else:
            votes["microprice"] = 0
            details["microprice"] = f"0(diff={microprice_diff:.4f})"

        # 4. Brownian P(up)
        remaining_sec = max(state.remaining_sec, 1)
        remaining_min = remaining_sec / 60.0
        sigma_remaining = cfg.brownian_sigma * math.sqrt(remaining_min)
        if sigma_remaining > 0:
            z = spot_return_frac / sigma_remaining
            p_brownian = _phi(z)
        else:
            p_brownian = 0.5

        if p_brownian > cfg.brownian_yes_threshold:
            votes["brownian"] = 1
            details["brownian"] = f"+1(P={p_brownian:.3f})"
        elif p_brownian < cfg.brownian_no_threshold:
            votes["brownian"] = -1
            details["brownian"] = f"-1(P={p_brownian:.3f})"
        else:
            votes["brownian"] = 0
            details["brownian"] = f"0(P={p_brownian:.3f})"

        # 5. Book depth asymmetry
        bid_depth = sum(size for _, size in state.bids)
        ask_depth = sum(size for _, size in state.asks)
        if bid_depth > 0 and ask_depth > 0:
            log_depth_ratio = math.log(bid_depth / ask_depth)
        else:
            log_depth_ratio = 0.0

        if log_depth_ratio > cfg.depth_dead_zone:
            votes["depth"] = 1
            details["depth"] = f"+1(ratio={log_depth_ratio:.2f})"
        elif log_depth_ratio < -cfg.depth_dead_zone:
            votes["depth"] = -1
            details["depth"] = f"-1(ratio={log_depth_ratio:.2f})"
        else:
            votes["depth"] = 0
            details["depth"] = f"0(ratio={log_depth_ratio:.2f})"

        # -- Tally votes --
        yes_votes = sum(1 for v in votes.values() if v == 1)
        no_votes = sum(1 for v in votes.values() if v == -1)
        total_signals = len(votes)  # always 5

        if max(yes_votes, no_votes) < cfg.min_signals_agree:
            vote_str = " ".join(f"{k}={details[k]}" for k in votes)
            return Signal("hold", 0, 0,
                          f"no consensus: Y={yes_votes} N={no_votes} [{vote_str}]")

        # -- Direction --
        if yes_votes >= no_votes:
            direction = "yes"
            vote_count = yes_votes
        else:
            direction = "no"
            vote_count = no_votes

        # -- Probability estimate from vote strength --
        # Scale conviction: more agreement = higher confidence
        p_hat = 0.5 + (vote_count / total_signals) * 0.2
        p_hat = max(0.02, min(0.98, p_hat))

        if direction == "no":
            # p_hat represents our confidence in the chosen direction
            # For NO, flip: if we're confident in NO, P(YES) is low
            p_yes = 1.0 - p_hat
        else:
            p_yes = p_hat

        # -- Divergence from market --
        p_market = state.midpoint
        divergence_bps = (p_yes - p_market) * 10_000

        if abs(divergence_bps) < cfg.min_edge_bps:
            vote_str = " ".join(f"{k}={details[k]}" for k in votes)
            return Signal("hold", 0, 0,
                          f"edge too small: div={divergence_bps:.0f}bp [{vote_str}]",
                          p_hat=p_yes, ev_bps=divergence_bps)

        # -- Action and entry price --
        if divergence_bps > 0:
            action = "buy_yes"
            entry_price = state.best_ask if state.best_ask > 0 else p_market
            p_kelly = p_yes
        else:
            action = "buy_no"
            entry_price = 1.0 - (state.best_bid if state.best_bid > 0 else p_market)
            p_kelly = 1.0 - p_yes

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
                          f"-EV: {ev:.4f}",
                          p_hat=p_yes, ev_bps=divergence_bps)

        f_star = ev / eff_profit
        f = max(0.0, min(1.0, f_star * cfg.kelly_fraction))
        size = int(cfg.bankroll * f / entry_price) if entry_price > 0.01 else 0
        size = min(size, cfg.max_size)
        size = max(size, 1) if f > 0 else 0

        if size == 0:
            return Signal("hold", 0, 0, "kelly=0")

        vote_str = " ".join(f"{k}={details[k]}" for k in votes)
        rationale = (
            f"consensus {vote_count}/{total_signals}: {action} "
            f"p={p_yes:.3f} mkt={p_market:.2f} div={divergence_bps:.0f}bp "
            f"fee={fee_rate*100:.3f}% [{vote_str}]"
        )

        return Signal(
            action=action,
            size=size,
            max_slippage_bps=cfg.slippage_tolerance_bps,
            rationale=rationale,
            p_hat=p_yes,
            ev_bps=divergence_bps,
        )

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        self._spot_history.pop(condition_id, None)

    def reset(self):
        self._spot_history.clear()

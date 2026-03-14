"""
Risk engine. Sits between strategy signals and order execution.
All limits are config values — sweep them in backtests to find optimal.
"""

from dataclasses import dataclass
from typing import Tuple

from strategies.base import Signal, MarketState


@dataclass
class RiskConfig:
    """All risk parameters. Every field is testable via parameter sweep."""
    # Position limits
    max_position_per_market: int = 500
    max_total_exposure: float = 5000.0
    max_concurrent_positions: int = 6

    # Loss limits
    max_loss_per_window: float = 200.0
    max_drawdown_pct: float = 10.0

    # Market quality filters
    max_spread_bps: float = 500.0
    min_liquidity: float = 0.0

    # Timing
    min_remaining_sec: int = 30
    max_elapsed_sec: int = 240

    # Recovery
    cooldown_after_loss_sec: int = 0


class RiskEngine:
    """Validates signals against risk limits before execution."""

    def __init__(self, config: RiskConfig):
        self.config = config
        self._last_loss_ts: int = 0

    def check(self, signal: Signal, state: MarketState,
              open_position_count: int, total_exposure: float,
              session_pnl: float, bankroll: float,
              current_ts: int = 0) -> Tuple[bool, str]:
        """
        Check if a signal passes all risk limits.

        Returns:
            (allowed, reason) — reason is empty string if allowed,
            otherwise describes why the signal was blocked.
        """
        cfg = self.config

        if signal.action == "hold":
            return True, ""

        # Spread check
        if state.spread_bps > cfg.max_spread_bps:
            return False, f"spread {state.spread_bps:.0f} bps > max {cfg.max_spread_bps:.0f}"

        # Liquidity check
        if cfg.min_liquidity > 0:
            depth = state.ask_depth if signal.action == "buy_yes" else state.bid_depth
            if depth < cfg.min_liquidity:
                return False, f"depth ${depth:.0f} < min ${cfg.min_liquidity:.0f}"

        # Timing checks
        if state.remaining_sec < cfg.min_remaining_sec:
            return False, f"remaining {state.remaining_sec}s < min {cfg.min_remaining_sec}s"

        if state.elapsed_sec > cfg.max_elapsed_sec:
            return False, f"elapsed {state.elapsed_sec}s > max {cfg.max_elapsed_sec}s"

        # Position size check
        if signal.size > cfg.max_position_per_market:
            return False, f"size {signal.size} > max {cfg.max_position_per_market}"

        # Concurrent positions check
        if open_position_count >= cfg.max_concurrent_positions:
            return False, f"open positions {open_position_count} >= max {cfg.max_concurrent_positions}"

        # Total exposure check
        notional = signal.size * state.midpoint
        if total_exposure + notional > cfg.max_total_exposure:
            return False, f"exposure ${total_exposure + notional:.0f} > max ${cfg.max_total_exposure:.0f}"

        # Drawdown circuit breaker
        if bankroll > 0:
            drawdown_pct = (-session_pnl / bankroll) * 100 if session_pnl < 0 else 0
            if drawdown_pct > cfg.max_drawdown_pct:
                return False, f"drawdown {drawdown_pct:.1f}% > max {cfg.max_drawdown_pct:.1f}%"

        # Cooldown after loss
        if cfg.cooldown_after_loss_sec > 0 and self._last_loss_ts > 0:
            elapsed_since_loss = (current_ts - self._last_loss_ts) / 1000  # ms to sec
            if elapsed_since_loss < cfg.cooldown_after_loss_sec:
                return False, f"cooldown: {elapsed_since_loss:.0f}s < {cfg.cooldown_after_loss_sec}s"

        return True, ""

    def record_loss(self, ts: int):
        """Record timestamp of a losing trade for cooldown logic."""
        self._last_loss_ts = ts

    def reset(self):
        """Reset state between runs."""
        self._last_loss_ts = 0

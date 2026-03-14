"""
Order manager: processes signals through risk checks and fill simulation.
Bridges strategy output to portfolio updates.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from strategies.base import Signal, MarketState, Position
from execution.fees import taker_fee
from execution.fill_simulator import simulate_fill, FillResult
from execution.risk_engine import RiskEngine
from execution.portfolio import Portfolio

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result of processing a signal through the order manager."""
    signal: Signal
    allowed: bool
    block_reason: str
    fill: Optional[FillResult]
    position: Optional[Position]


class OrderManager:
    """
    Processes signals: risk check -> fill simulation -> portfolio update.
    """

    def __init__(self, risk_engine: RiskEngine, portfolio: Portfolio):
        self.risk = risk_engine
        self.portfolio = portfolio
        self.results: List[OrderResult] = []
        self.blocked_count: int = 0

    def process_signal(
        self,
        signal: Signal,
        state: MarketState,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
    ) -> OrderResult:
        """
        Process a strategy signal end-to-end.

        1. Risk check
        2. Fill simulation against order book
        3. Portfolio update

        Returns OrderResult with full details.
        """
        if signal.action == "hold":
            result = OrderResult(signal=signal, allowed=True, block_reason="",
                                 fill=None, position=None)
            self.results.append(result)
            return result

        # Risk check
        allowed, reason = self.risk.check(
            signal=signal,
            state=state,
            open_position_count=self.portfolio.open_position_count,
            total_exposure=self.portfolio.total_exposure,
            session_pnl=self.portfolio.session_pnl,
            bankroll=self.portfolio.bankroll,
            current_ts=state.ts,
        )

        if not allowed:
            self.blocked_count += 1
            log.debug("Signal blocked: %s", reason)
            result = OrderResult(signal=signal, allowed=False, block_reason=reason,
                                 fill=None, position=None)
            self.results.append(result)
            return result

        # Check if we already have a position in this market
        if state.condition_id in self.portfolio.positions:
            result = OrderResult(signal=signal, allowed=False,
                                 block_reason="already positioned in this market",
                                 fill=None, position=None)
            self.results.append(result)
            return result

        # Fill simulation
        fill = simulate_fill(
            side=signal.action,
            size=signal.size,
            bids=bids,
            asks=asks,
            max_slippage_bps=signal.max_slippage_bps,
        )

        if not fill.filled:
            self.blocked_count += 1
            reason = f"fill failed: slippage={fill.slippage_bps:.0f}bps" if fill.slippage_bps > signal.max_slippage_bps else "no liquidity"
            log.debug("Fill rejected: %s", reason)
            result = OrderResult(signal=signal, allowed=True, block_reason=reason,
                                 fill=fill, position=None)
            self.results.append(result)
            return result

        # Check bankroll
        if fill.total_cost > self.portfolio.bankroll:
            self.blocked_count += 1
            result = OrderResult(signal=signal, allowed=False,
                                 block_reason=f"insufficient bankroll: need ${fill.total_cost:.2f}, have ${self.portfolio.bankroll:.2f}",
                                 fill=fill, position=None)
            self.results.append(result)
            return result

        # Execute: update portfolio
        side = "yes" if signal.action == "buy_yes" else "no"
        position = self.portfolio.open_position(
            condition_id=state.condition_id,
            yes_token_id=state.yes_token_id,
            asset=state.asset,
            slug=state.slug,
            side=side,
            size=fill.filled_size,
            fill_price=fill.avg_price,
            fee=fill.fee,
            ts=state.ts,
            window_end_ts=state.window_end_ts,
            slippage_bps=fill.slippage_bps,
            rationale=signal.rationale,
        )

        log.info("%s %s %.0f @ %.4f (fee=$%.4f, slip=%.0fbps) %s",
                 signal.action.upper(), state.asset, fill.filled_size,
                 fill.avg_price, fill.fee, fill.slippage_bps, signal.rationale)

        result = OrderResult(signal=signal, allowed=True, block_reason="",
                             fill=fill, position=position)
        self.results.append(result)
        return result

    def summary(self) -> dict:
        """Summary of order manager activity."""
        executed = [r for r in self.results if r.position is not None]
        blocked = [r for r in self.results if not r.allowed or (r.fill and not r.fill.filled)]
        holds = [r for r in self.results if r.signal.action == "hold"]
        return {
            "total_signals": len(self.results),
            "executed": len(executed),
            "blocked": len(blocked),
            "holds": len(holds),
            "blocked_reasons": [r.block_reason for r in blocked if r.block_reason],
        }

    def reset(self):
        """Reset for a new run."""
        self.results.clear()
        self.blocked_count = 0

"""
Portfolio: tracks positions, PnL, settlement, bankroll.
Handles both hold-to-expiry and dynamic exit flows.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from strategies.base import Position


@dataclass
class Trade:
    """Record of a completed trade (entry or exit)."""
    ts: int                         # unix ms
    condition_id: str
    asset: str
    slug: str
    side: Literal["buy_yes", "buy_no", "sell_yes", "sell_no"]
    size: float
    fill_price: float
    slippage_bps: float
    fee: float
    rationale: str


@dataclass
class SettledPosition:
    """A position that has been resolved."""
    position: Position
    outcome: str                    # "yes" or "no"
    payout: float                   # total payout (size * 1.0 if won, 0 if lost)
    pnl: float                      # payout - cost - fees
    settled_ts: int


class Portfolio:
    """Tracks open positions, closed trades, and PnL."""

    def __init__(self, bankroll: float = 10_000.0):
        self.initial_bankroll = bankroll
        self.bankroll = bankroll
        self.positions: Dict[str, Position] = {}     # condition_id -> Position
        self.trades: List[Trade] = []
        self.settled: List[SettledPosition] = []
        self.total_fees: float = 0.0

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    @property
    def total_exposure(self) -> float:
        """Total capital at risk across all open positions."""
        return sum(p.size * p.entry_price for p in self.positions.values())

    @property
    def session_pnl(self) -> float:
        """Realized PnL from settled positions."""
        return sum(s.pnl for s in self.settled)

    @property
    def net_pnl(self) -> float:
        """Net PnL including unrealized (mark open positions to midpoint)."""
        return self.bankroll - self.initial_bankroll

    def open_position(self, condition_id: str, yes_token_id: str, asset: str,
                      slug: str, side: Literal["yes", "no"], size: float,
                      fill_price: float, fee: float, ts: int,
                      window_end_ts: int, slippage_bps: float = 0.0,
                      rationale: str = "") -> Position:
        """Record a new position entry."""
        cost = size * fill_price + fee
        self.bankroll -= cost
        self.total_fees += fee

        pos = Position(
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            asset=asset,
            slug=slug,
            side=side,
            size=int(size),
            entry_price=fill_price,
            entry_ts=ts,
            entry_fee=fee,
            window_end_ts=window_end_ts,
        )
        self.positions[condition_id] = pos

        trade_side = f"buy_{side}"
        self.trades.append(Trade(
            ts=ts,
            condition_id=condition_id,
            asset=asset,
            slug=slug,
            side=trade_side,
            size=size,
            fill_price=fill_price,
            slippage_bps=slippage_bps,
            fee=fee,
            rationale=rationale,
        ))

        return pos

    def settle(self, condition_id: str, outcome: str, ts: int) -> Optional[SettledPosition]:
        """
        Settle a position when a market resolves.

        Args:
            condition_id: which market resolved
            outcome: "yes" or "no"
            ts: settlement timestamp (unix ms)

        Returns:
            SettledPosition if we had a position, None otherwise.
        """
        pos = self.positions.pop(condition_id, None)
        if pos is None:
            return None

        # Did we win?
        won = (pos.side == outcome)
        payout = pos.size * 1.0 if won else 0.0
        cost = pos.size * pos.entry_price + pos.entry_fee
        pnl = payout - cost

        self.bankroll += payout

        settled = SettledPosition(
            position=pos,
            outcome=outcome,
            payout=payout,
            pnl=pnl,
            settled_ts=ts,
        )
        self.settled.append(settled)
        return settled

    def close_position(self, condition_id: str, fill_price: float, fee: float,
                       ts: int, slippage_bps: float = 0.0) -> Optional[float]:
        """
        Close a position before expiry (dynamic exit).
        Returns PnL of the closed position, or None if no position.
        """
        pos = self.positions.pop(condition_id, None)
        if pos is None:
            return None

        proceeds = pos.size * fill_price - fee
        cost = pos.size * pos.entry_price + pos.entry_fee
        pnl = proceeds - cost

        self.bankroll += proceeds
        self.total_fees += fee

        sell_side = f"sell_{pos.side}"
        self.trades.append(Trade(
            ts=ts,
            condition_id=condition_id,
            asset=pos.asset,
            slug=pos.slug,
            side=sell_side,
            size=pos.size,
            fill_price=fill_price,
            slippage_bps=slippage_bps,
            fee=fee,
            rationale="dynamic_exit",
        ))

        # Record as settled with the exit
        self.settled.append(SettledPosition(
            position=pos,
            outcome="exit",
            payout=proceeds,
            pnl=pnl,
            settled_ts=ts,
        ))

        return pnl

    def summary(self) -> dict:
        """Return a summary of portfolio state."""
        wins = [s for s in self.settled if s.pnl > 0]
        losses = [s for s in self.settled if s.pnl <= 0]
        return {
            "bankroll": round(self.bankroll, 2),
            "initial_bankroll": self.initial_bankroll,
            "net_pnl": round(self.net_pnl, 2),
            "session_pnl": round(self.session_pnl, 2),
            "total_fees": round(self.total_fees, 4),
            "open_positions": self.open_position_count,
            "total_trades": len(self.trades),
            "settled_count": len(self.settled),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self.settled) * 100, 1) if self.settled else 0.0,
            "total_exposure": round(self.total_exposure, 2),
        }

    def reset(self, bankroll: Optional[float] = None):
        """Reset for a new run."""
        self.bankroll = bankroll if bankroll is not None else self.initial_bankroll
        self.initial_bankroll = self.bankroll
        self.positions.clear()
        self.trades.clear()
        self.settled.clear()
        self.total_fees = 0.0

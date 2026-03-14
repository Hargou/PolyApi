"""
Strategy interface and core data types.
All strategies implement BaseStrategy.evaluate() -> Signal.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional


class ExitPolicy(Enum):
    HOLD_TO_EXPIRY = "hold"
    DYNAMIC = "dynamic"


@dataclass
class MarketState:
    """Snapshot of market + spot at a point in time. Built by StrategyRunner."""
    # Market identity
    condition_id: str
    yes_token_id: str
    asset: str  # BTC, ETH, SOL
    slug: str

    # Order book
    best_bid: float
    best_ask: float
    spread: float           # best_ask - best_bid
    spread_bps: float       # spread / midpoint * 10000
    midpoint: float         # (best_bid + best_ask) / 2
    bid_depth: float        # total size on bid side (dollars)
    ask_depth: float        # total size on ask side (dollars)

    # Spot price
    spot_price: float
    spot_price_at_window_start: float
    spot_return_bps: float  # (current - start) / start * 10000

    # Timing
    window_start_ts: int    # unix seconds
    window_end_ts: int      # window_start_ts + 300
    elapsed_sec: int
    remaining_sec: int
    ts: int                 # current event timestamp (unix ms)


@dataclass
class Signal:
    """Output of a strategy evaluation."""
    action: Literal["buy_yes", "buy_no", "hold"]
    size: int               # contract count
    max_slippage_bps: int   # reject fill if slippage exceeds this
    rationale: str
    p_hat: Optional[float] = None   # estimated P(Yes)
    ev_bps: Optional[float] = None  # edge in bps: (p_hat - p_market) * 10000


@dataclass
class Position:
    """A position in a single market."""
    condition_id: str
    yes_token_id: str
    asset: str
    slug: str
    side: Literal["yes", "no"]
    size: int               # contracts held
    entry_price: float      # average fill price
    entry_ts: int            # when entered (unix ms)
    entry_fee: float        # total fee paid on entry
    window_end_ts: int      # when this market resolves


class BaseStrategy(ABC):
    """All strategies implement this interface."""

    name: str = "unnamed"
    exit_policy: ExitPolicy = ExitPolicy.HOLD_TO_EXPIRY

    @abstractmethod
    def evaluate(self, state: MarketState) -> Signal:
        """Produce a trading signal given current market state."""
        ...

    def should_exit(self, state: MarketState, position: Position) -> bool:
        """Override for DYNAMIC exit policy. Called every tick if exit_policy == DYNAMIC."""
        return False

    def on_market_resolved(self, condition_id: str, outcome: str, pnl: float):
        """Optional hook: called when a market resolves."""
        pass

    def reset(self):
        """Reset any internal state between backtest runs."""
        pass

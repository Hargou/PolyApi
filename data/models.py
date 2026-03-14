"""
Data models for events flowing through the system.
Used by both ReplaySource (backtest) and LiveSource (paper trading).
"""

from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple


@dataclass
class SpotTick:
    """A spot price update."""
    ts: int                 # unix ms
    symbol: str             # btcusdt, ethusdt, solusdt
    price: float


@dataclass
class BookLevel:
    """A single price level in the order book."""
    price: float
    size: float


@dataclass
class ClobSnapshot:
    """Order book snapshot for a single token."""
    ts: int                 # unix ms
    asset_id: str           # token ID
    condition_id: str
    bids: List[Tuple[float, float]]   # [(price, size), ...] highest first
    asks: List[Tuple[float, float]]   # [(price, size), ...] lowest first
    best_bid: float
    best_ask: float
    last_trade_price: Optional[float] = None


@dataclass
class MarketInfo:
    """Metadata about a discovered market."""
    condition_id: str
    yes_token_id: str
    asset: str              # BTC, ETH, SOL
    slug: str
    window_start_ts: int    # unix seconds (parsed from slug)
    window_end_ts: int      # window_start_ts + 300
    question: str = ""
    volume: float = 0.0
    liquidity: float = 0.0


@dataclass
class MarketResolution:
    """A market resolution event."""
    ts: int                 # unix ms
    condition_id: str
    outcome: str            # "yes" or "no"


@dataclass
class Event:
    """
    Unified event wrapper. The StrategyRunner processes these sequentially.
    Exactly one of the payload fields will be set.
    """
    ts: int                                 # unix ms — used for ordering
    type: Literal["spot", "clob", "market_info", "resolution"]
    spot: Optional[SpotTick] = None
    clob: Optional[ClobSnapshot] = None
    market_info: Optional[MarketInfo] = None
    resolution: Optional[MarketResolution] = None

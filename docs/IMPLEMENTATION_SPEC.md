# Implementation Specification: Backtesting & Paper Trading

**Version:** 1.0  
**Date:** 2026-03-10  
**Scope:** Complete specification for Phase 1-5 implementation

---

## 1. System Architecture

### 1.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              APPLICATION LAYER                               │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │
│  │   Recorder   │  │   Backtest   │  │    Paper     │  │   Strategy Lab   │ │
│  │   Service    │  │    Runner    │  │    Trader    │  │       UI         │ │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────────────────┘ │
│         │                 │                 │                                │
│  ┌──────▼─────────────────▼─────────────────▼────────────────────────────┐  │
│  │                      STRATEGY RUNTIME                                  │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │  │
│  │  │ Data Source │→ │   Signal    │→ │  Risk       │→ │   Order     │  │  │
│  │  │  (Live/     │  │   Layer     │  │  Engine     │  │  Manager    │  │  │
│  │  │   Replay)   │  │ (Strategies)│  │             │  │             │  │  │
│  │  └─────────────┘  └─────────────┘  └──────┬──────┘  └──────┬──────┘  │  │
│  │                                           │                │        │  │
│  │                              ┌────────────▼────────────────▼────┐   │  │
│  │                              │         PORTFOLIO                │   │  │
│  │                              │  (Positions, PnL, Settlement)    │   │  │
│  │                              └──────────────────────────────────┘   │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼─────────────────────────────────────────┐
│                              DATA LAYER                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │ Live Source  │  │Replay Source │  │   Event      │  │    Run       │     │
│  │   Adapter    │  │   Adapter    │  │   Store      │  │   Store      │     │
│  │              │  │              │  │ (Parquet/    │  │ (SQLite/     │     │
│  │              │  │              │  │   JSONL)     │  │   JSON)      │     │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Module Structure

```
polyapi/
├── app.py                          # FastAPI app (existing)
├── engines/                        # Existing: price, market, feed
│
├── strategies/                     # NEW: Strategy implementations
│   ├── __init__.py
│   ├── base.py                     # BaseStrategy, MarketState, Signal
│   ├── registry.py                 # Strategy discovery & loading
│   ├── state.py                    # Strategy state management
│   ├── sizing.py                   # Kelly sizing, position sizing
│   └── implementations/            # Concrete strategies
│       ├── __init__.py
│       ├── spot_momentum.py        # Spot vs market edge
│       ├── buy_and_hold.py         # Benchmark
│       └── random_baseline.py      # Benchmark
│
├── execution/                      # NEW: Trading engine
│   ├── __init__.py
│   ├── runner.py                   # StrategyRunner (live + replay)
│   ├── context.py                  # StrategyContext injection
│   ├── portfolio.py                # Position tracking, PnL
│   ├── order_manager.py            # Order lifecycle
│   ├── risk_engine.py              # Risk limits & checks
│   ├── fill_simulator.py           # L2-aware fill simulation
│   └── fees.py                     # Fee models
│
├── data/                           # NEW: Data layer
│   ├── __init__.py
│   ├── models.py                   # Data classes (Event, Trade, etc.)
│   ├── live_source.py              # Adapter for PriceEngine/MarketEngine
│   ├── replay_source.py            # Event replay from storage
│   ├── recorder.py                 # Event recording service
│   ├── event_store.py              # Read/write event storage
│   └── run_store.py                # Backtest run persistence
│
├── analysis/                       # NEW: Metrics & reporting
│   ├── __init__.py
│   ├── metrics.py                  # Sharpe, drawdown, etc.
│   ├── reporting.py                # Report generation
│   └── benchmarks.py               # Benchmark comparisons
│
├── cli/                            # NEW: Command-line interface
│   ├── __init__.py
│   ├── record.py                   # Start/stop recording
│   ├── backtest.py                 # Run backtest
│   ├── paper.py                    # Run paper trading
│   └── analyze.py                  # Analyze past runs
│
├── config/                         # NEW: Configuration
│   ├── __init__.py
│   ├── settings.py                 # Pydantic settings
│   └── strategies/                 # Strategy configs
│       ├── spot_momentum.yaml
│       └── default.yaml
│
└── data_store/                     # NEW: Gitignored data directory
    ├── events/                     # Recorded market events
    │   ├── 20250309_12.jsonl
    │   └── 20250309_13.jsonl
    ├── runs/                       # Backtest/paper run results
    │   ├── run_abc123/
    │   │   ├── metadata.json
    │   │   ├── trades.csv
    │   │   └── equity_curve.csv
    └── cache/                      # Downloaded historical data
```

---

## 2. Core Data Models

### 2.1 Event Model (for Recording & Replay)

```python
# data/models.py

from dataclasses import dataclass, field, asdict
from typing import Literal, Optional
from datetime import datetime
import json

@dataclass(frozen=True)
class Event:
    """Base event for recording and replay. Immutable."""
    timestamp_ns: int                    # Nanoseconds since epoch
    event_type: Literal[
        "spot_price",                   # Coinbase price tick
        "clob_book",                    # Full order book snapshot
        "clob_best_bid_ask",            # Top of book update
        "clob_trade",                   # Trade execution
        "market_list",                  # Market discovery update
        "market_resolution",            # Market settled
    ]
    source: str                          # "coinbase", "polymarket_clob", "gamma"
    asset: Optional[str]                 # "BTC", "ETH", "SOL", or None
    data: dict                          # Event-specific payload
    
    def to_json(self) -> str:
        return json.dumps({
            "ts": self.timestamp_ns,
            "type": self.event_type,
            "source": self.source,
            "asset": self.asset,
            "data": self.data,
        }, default=str)
    
    @classmethod
    def from_json(cls, line: str) -> "Event":
        d = json.loads(line)
        return cls(
            timestamp_ns=d["ts"],
            event_type=d["type"],
            source=d["source"],
            asset=d.get("asset"),
            data=d["data"],
        )

@dataclass
class SpotPriceEvent:
    """Payload for event_type='spot_price'"""
    symbol: str                          # "BTC-USD"
    price: float
    size_24h: Optional[float] = None
    
@dataclass  
class ClobBookEvent:
    """Payload for event_type='clob_book'"""
    asset_id: str
    bids: list[tuple[float, float]]     # [(price, size), ...]
    asks: list[tuple[float, float]]
    
@dataclass
class ClobTradeEvent:
    """Payload for event_type='clob_trade'"""
    asset_id: str
    price: float
    size: float
    side: Literal["BUY", "SELL"]
    timestamp_ns: int
```

### 2.2 Strategy Interface

```python
# strategies/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Optional, Any
from decimal import Decimal

@dataclass(frozen=True)
class MarketState:
    """Immutable snapshot of market conditions."""
    # Identification
    condition_id: str
    yes_token_id: str
    asset: Literal["BTC", "ETH", "SOL"]
    
    # Market data
    best_bid: Decimal                    # Decimal for precision
    best_ask: Decimal
    spread_bps: int                      # Basis points
    
    # Spot data
    spot_price: Decimal
    spot_price_at_window_start: Decimal  # For return calculation
    spot_return_bps: int                 # Pre-calculated
    
    # Timing
    window_start_ts: int                 # Unix seconds
    window_end_ts: int
    elapsed_sec: int
    remaining_sec: int
    
    # Liquidity (optional, for sizing)
    bid_depth_5pct: Optional[Decimal] = None   # Contracts within 5% of mid
    ask_depth_5pct: Optional[Decimal] = None
    volume_1h: Optional[Decimal] = None
    
    # Book snapshot (for fill simulation)
    book: Optional["OrderBook"] = None

@dataclass(frozen=True)
class Signal:
    """Immutable trading signal from strategy."""
    action: Literal["buy_yes", "buy_no", "sell_yes", "sell_no", "hold"]
    
    # Sizing
    size: int                            # Number of contracts (positive int)
    size_basis: Literal["fixed", "kelly", "risk_pct"] = "fixed"
    
    # Limits
    max_slippage_bps: int = 50           # Reject if slippage exceeds this
    max_hold_sec: Optional[int] = None   # Force exit after N seconds
    
    # Metadata
    rationale: str = ""
    p_hat: Optional[Decimal] = None      # Your estimated probability
    ev_bps: Optional[int] = None         # Edge in basis points
    confidence: Optional[float] = None   # 0-1 confidence in signal
    
    # Exit policy
    exit_policy: Literal["hold_to_expiry", "trailing_stop", "take_profit"] = "hold_to_expiry"
    exit_params: dict = field(default_factory=dict)

@dataclass
class OrderBook:
    """L2 order book snapshot."""
    asset_id: str
    timestamp_ns: int
    bids: list[tuple[Decimal, Decimal]]  # [(price, size), ...] sorted best first
    asks: list[tuple[Decimal, Decimal]]
    
    @property
    def mid(self) -> Decimal:
        if not self.bids or not self.asks:
            return Decimal("0")
        return (self.bids[0][0] + self.asks[0][0]) / 2
    
    def walk_book(self, side: Literal["buy", "sell"], size: Decimal) -> tuple[Decimal, Decimal]:
        """
        Walk the book for a given size.
        Returns (avg_fill_price, actual_size_filled).
        """
        levels = self.asks if side == "buy" else self.bids
        remaining = size
        notional = Decimal("0")
        filled = Decimal("0")
        
        for price, level_size in levels:
            take = min(remaining, level_size)
            notional += take * price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
                
        avg_price = notional / filled if filled > 0 else Decimal("0")
        return avg_price, filled

class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.
    
    Thread-safety: Implementations should be stateless or use immutable state.
    State mutations should return new state objects, not modify in place.
    """
    
    def __init__(self, config: dict[str, Any]):
        """
        Initialize with configuration. Do not perform I/O here.
        """
        self.config = config
        self.name = self.__class__.__name__
        self.version = config.get("version", "1.0")
        
    @abstractmethod
    def evaluate(self, state: MarketState, context: "StrategyContext") -> Signal:
        """
        Evaluate market state and return a trading signal.
        
        This method must be pure (no side effects) for backtest reproducibility.
        """
        pass
    
    def save_state(self) -> dict:
        """
        Serialize strategy state for checkpointing.
        Called periodically during live trading, at end of backtest windows.
        """
        return {}
    
    def load_state(self, state: dict) -> None:
        """Restore strategy state from checkpoint."""
        pass
    
    def on_market_resolved(self, condition_id: str, outcome: bool, payout: Decimal) -> None:
        """
        Called when a market resolves. Use for post-hoc analysis,
        updating internal models, etc.
        """
        pass
    
    def reset(self) -> None:
        """Reset to initial state. Called between backtest runs."""
        pass

@dataclass
class StrategyContext:
    """
    Injectable dependencies for strategies.
    Provides access to portfolio, risk limits, etc. without tight coupling.
    """
    portfolio: "Portfolio"
    clock: "Clock"
    config: dict[str, Any]
    
    # Callbacks for advanced strategies
    get_historical_spot: Optional[callable] = None  # For lookback strategies
```

### 2.3 Portfolio & Execution Models

```python
# execution/portfolio.py

from dataclasses import dataclass, field
from typing import Literal, Optional
from decimal import Decimal
from datetime import datetime
from enum import Enum

class PositionStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLING = "settling"    # Waiting for market resolution
    SETTLED = "settled"

@dataclass
class Position:
    """A single position (trade)."""
    id: str                              # UUID
    condition_id: str
    asset: str
    side: Literal["yes", "no"]
    entry_price: Decimal                 # Price paid (0-1)
    size: int                            # Number of contracts
    
    # Timing
    entry_ts: int                        # Unix seconds
    exit_ts: Optional[int] = None
    expiry_ts: int                       # When market resolves
    
    # Status
    status: PositionStatus = PositionStatus.OPEN
    
    # Exit details
    exit_price: Optional[Decimal] = None
    exit_reason: Optional[str] = None    # "expiry", "stop_loss", "signal", etc.
    
    # PnL (calculated at exit)
    gross_pnl: Optional[Decimal] = None
    fees_paid: Optional[Decimal] = None
    net_pnl: Optional[Decimal] = None
    
    def calculate_pnl(self, exit_price: Decimal, fee_model: "FeeModel") -> Decimal:
        """Calculate PnL if exited at given price."""
        notional = self.size * exit_price
        gross = self.size * (exit_price - self.entry_price) if self.side == "yes" \
                else self.size * (self.entry_price - exit_price)
        fees = fee_model.calculate_exit_fee(notional, gross)
        return gross - fees

@dataclass
class Portfolio:
    """Tracks all positions and cash."""
    initial_cash: Decimal
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)  # id -> Position
    
    # Performance tracking
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_fees: Decimal = field(default_factory=lambda: Decimal("0"))
    
    # Equity curve (timestamp_ns, value)
    equity_curve: list[tuple[int, Decimal]] = field(default_factory=list)
    
    def update_equity(self, timestamp_ns: int, market_prices: dict[str, Decimal]) -> None:
        """Update equity curve with current unrealized PnL."""
        unrealized = Decimal("0")
        for pos in self.positions.values():
            if pos.status == PositionStatus.OPEN and pos.condition_id in market_prices:
                price = market_prices[pos.condition_id]
                # Simplified - doesn't account for fees
                if pos.side == "yes":
                    unrealized += pos.size * (price - pos.entry_price)
                else:
                    unrealized += pos.size * (pos.entry_price - price)
        
        total_value = self.cash + unrealized
        self.equity_curve.append((timestamp_ns, total_value))
```

### 2.4 Risk Management

```python
# execution/risk_engine.py

from dataclasses import dataclass
from typing import Optional, Literal
from decimal import Decimal

@dataclass(frozen=True)
class RiskLimits:
    """Immutable risk configuration."""
    # Per-market limits
    max_position_per_market: int = 1000
    max_order_size: int = 100
    
    # Portfolio limits
    max_total_exposure: int = 3000        # Total contracts across all markets
    max_cash_utilization_pct: float = 0.8  # Don't use more than 80% of cash
    
    # Drawdown controls
    max_drawdown_pct: float = 0.1         # Stop at 10% DD
    daily_loss_limit: Optional[Decimal] = None
    
    # Rate limits
    max_trades_per_hour: int = 50
    max_trades_per_market_per_hour: int = 10
    
    # Correlation (simple version)
    max_correlated_exposure: int = 2000   # BTC+ETH+SOL often move together
    
    # Liquidity
    max_position_as_pct_of_volume: float = 0.05  # Don't be >5% of 1h volume

@dataclass
class RiskCheckResult:
    allowed: bool
    reason: Optional[str] = None
    adjusted_size: Optional[int] = None   # Risk engine can suggest smaller size

class RiskEngine:
    """
    Centralized risk management.
    Stateless - all state passed in via Portfolio and context.
    """
    
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        
    def check_signal(
        self, 
        signal: "Signal", 
        state: "MarketState",
        portfolio: "Portfolio",
        recent_trades: list["Trade"],
    ) -> RiskCheckResult:
        """
        Check if a signal should be allowed.
        Returns RiskCheckResult with decision and optional adjustment.
        """
        # Check 1: Drawdown circuit breaker
        if portfolio.equity_curve:
            peak = max(v for _, v in portfolio.equity_curve)
            current = portfolio.equity_curve[-1][1]
            drawdown = (peak - current) / peak
            if drawdown > self.limits.max_drawdown_pct:
                return RiskCheckResult(
                    allowed=False, 
                    reason=f"Drawdown circuit breaker: {drawdown:.2%} > {self.limits.max_drawdown_pct:.2%}"
                )
        
        # Check 2: Position size limit
        current_position = sum(
            p.size for p in portfolio.positions.values()
            if p.condition_id == state.condition_id and p.status.value == "open"
        )
        new_size = current_position + signal.size
        if new_size > self.limits.max_position_per_market:
            adjusted = self.limits.max_position_per_market - current_position
            if adjusted <= 0:
                return RiskCheckResult(
                    allowed=False,
                    reason=f"Max position limit reached for {state.condition_id}"
                )
            return RiskCheckResult(
                allowed=True,
                adjusted_size=adjusted,
                reason=f"Size adjusted from {signal.size} to {adjusted} due to position limit"
            )
        
        # Check 3: Total exposure
        total_exposure = sum(
            p.size for p in portfolio.positions.values()
            if p.status.value == "open"
        )
        if total_exposure + signal.size > self.limits.max_total_exposure:
            return RiskCheckResult(
                allowed=False,
                reason=f"Total exposure limit: {total_exposure + signal.size} > {self.limits.max_total_exposure}"
            )
        
        # Check 4: Rate limiting
        recent_count = len([t for t in recent_trades 
                           if t.timestamp_ns > datetime.now().timestamp() * 1e9 - 3600e9])
        if recent_count >= self.limits.max_trades_per_hour:
            return RiskCheckResult(allowed=False, reason="Rate limit: max trades per hour")
        
        # Check 5: Liquidity
        if state.bid_depth_5pct and signal.size > state.bid_depth_5pct * Decimal("0.5"):
            return RiskCheckResult(
                allowed=True,
                adjusted_size=int(state.bid_depth_5pct * Decimal("0.5")),
                reason="Adjusted for liquidity"
            )
        
        return RiskCheckResult(allowed=True)
```

### 2.5 Fee Model

```python
# execution/fees.py

from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class FeeModel:
    """
    Polymarket fee structure.
    As of 2026: 0% entry, 2% on positive returns at settlement.
    """
    entry_fee_pct: Decimal = Decimal("0")           # No entry fee
    exit_fee_pct: Decimal = Decimal("0")            # No explicit exit fee
    settlement_fee_pct: Decimal = Decimal("0.02")   # 2% on profits
    
    def calculate_entry_fee(self, notional: Decimal) -> Decimal:
        """Fee paid when entering position."""
        return notional * self.entry_fee_pct
    
    def calculate_exit_fee(self, notional: Decimal, gross_pnl: Decimal) -> Decimal:
        """
        Fee paid at settlement.
        Only paid on positive returns.
        """
        if gross_pnl > 0:
            return gross_pnl * self.settlement_fee_pct
        return Decimal("0")
    
    def calculate_total_fee(self, entry_notional: Decimal, exit_notional: Decimal, 
                           gross_pnl: Decimal) -> Decimal:
        """Total fees for a round-trip trade."""
        return (self.calculate_entry_fee(entry_notional) + 
                self.calculate_exit_fee(exit_notional, gross_pnl))

# Convenience instance
POLYMARKET_FEES = FeeModel()
```

---

## 3. Implementation Phases

### Phase 1: Data Infrastructure (Days 1-3)

**Goal:** Record live market data for backtesting.

#### 1.1 Event Model & Storage

```python
# File: data/models.py
# Implements: Event, EventType, serialization

# Tests: tests/test_event_model.py
# - Round-trip serialization
# - Timestamp precision
# - Immutable dataclass behavior
```

#### 1.2 Async Recorder Service

```python
# File: data/recorder.py
# Implements: EventRecorder with aiofiles

class EventRecorder:
    def __init__(self, base_path: str, rotation_interval_sec: int = 3600):
        ...
    
    async def record(self, event: Event) -> None:
        """Non-blocking write to current file."""
        ...
    
    async def start(self) -> None:
        """Begin recording, hook into engine callbacks."""
        ...
    
    async def stop(self) -> None:
        """Flush buffers, close files."""
        ...
```

**Key Design Decisions:**
- **Format:** JSONL for real-time (append-friendly), compact to Parquet hourly
- **Rotation:** Hourly files (`events_20250310_14.jsonl`)
- **Compression:** gzip files older than 24 hours
- **Buffering:** 100ms flush interval or 1000 events, whichever comes first

#### 1.3 Integration with Existing Engines

```python
# File: data/live_source.py

class LiveEventAdapter:
    """
    Adapts existing engine callbacks to Event objects.
    Plugs into app.py without modifying engines.
    """
    
    def __init__(self, recorder: EventRecorder):
        self.recorder = recorder
        
    def on_price(self, sym: str, value: float, ts: int):
        """Callback for PriceEngine."""
        event = Event(
            timestamp_ns=ts * 1_000_000,  # ms to ns
            event_type="spot_price",
            source="coinbase",
            asset=sym.replace("usdt", "").upper(),
            data={"symbol": sym, "price": value}
        )
        asyncio.create_task(self.recorder.record(event))
        
    def on_market_event(self, data: dict):
        """Callback for MarketEngine."""
        # Map CLOB event types to our Event types
        event_type = self._map_event_type(data.get("event_type"))
        ...
```

#### 1.4 CLI for Recording

```bash
# Usage examples:
python -m cli.record start --name "march_collection"
python -m cli.record status
python -m cli.record stop
python -m cli.record list                    # Show available recordings
python -m cli.record compact 20250309_12     # Convert JSONL to Parquet
```

**Deliverables:**
- [ ] `data/models.py` with full test coverage
- [ ] `data/recorder.py` with rotation and compression
- [ ] `data/live_source.py` adapter
- [ ] `cli/record.py` command interface
- [ ] Integration in `app.py` (optional recording flag)
- [ ] 1+ hours of recorded data for testing

---

### Phase 2: Strategy Runtime Core (Days 4-7)

**Goal:** Execute strategies against replayed or live data.

#### 2.1 Clock Abstraction

```python
# execution/context.py

from abc import ABC, abstractmethod

class Clock(ABC):
    """Abstract clock for time control."""
    
    @abstractmethod
    def now_ns(self) -> int:
        pass
    
    @abstractmethod
    def now_sec(self) -> int:
        pass

class RealTimeClock(Clock):
    """Normal operation - system clock."""
    def now_ns(self) -> int:
        return time.time_ns()

class SimulatedClock(Clock):
    """Backtesting - manual time advancement."""
    def __init__(self, start_ns: int):
        self._time = start_ns
        
    def advance(self, delta_ns: int):
        self._time += delta_ns
        
    def now_ns(self) -> int:
        return self._time
```

#### 2.2 Replay Source

```python
# data/replay_source.py

class ReplaySource:
    """
    Reads recorded events and emits them in chronological order.
    Controls simulated clock advancement.
    """
    
    def __init__(
        self, 
        event_files: list[str],
        speed: float = 1.0,          # 1.0 = real-time, 10.0 = 10x speed
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ):
        ...
    
    async def run(self, callback: Callable[[Event], None]) -> None:
        """
        Read events and emit via callback.
        Respects speed parameter for replay pacing.
        """
        ...
```

#### 2.3 Strategy Runner

```python
# execution/runner.py

class StrategyRunner:
    """
    Main execution loop. Works with LiveSource or ReplaySource.
    """
    
    def __init__(
        self,
        strategy: BaseStrategy,
        data_source: Union[LiveSource, ReplaySource],
        portfolio: Portfolio,
        risk_engine: RiskEngine,
        order_manager: OrderManager,
        clock: Clock,
    ):
        ...
    
    async def run(self) -> RunResult:
        """
        Main loop:
        1. Get next event from source
        2. Update market state
        3. Call strategy.evaluate()
        4. Risk check
        5. Execute via OrderManager
        6. Update portfolio
        """
        ...

@dataclass
class RunResult:
    """Summary of a backtest or paper run."""
    run_id: str
    mode: Literal["backtest", "paper"]
    strategy_name: str
    start_time: datetime
    end_time: datetime
    initial_cash: Decimal
    final_value: Decimal
    total_return_pct: float
    sharpe_ratio: Optional[float]
    max_drawdown_pct: float
    trade_count: int
    win_rate: float
    metadata: dict
```

#### 2.4 Portfolio & Settlement

```python
# execution/portfolio.py

class PortfolioManager:
    """
    Manages positions and handles settlement.
    """
    
    def open_position(self, signal: Signal, fill: Fill) -> Position:
        ...
    
    def close_position(self, position_id: str, exit_price: Decimal, 
                      reason: str) -> Position:
        ...
    
    def settle_market(self, condition_id: str, outcome: bool,
                     settlement_price: Decimal) -> list[Position]:
        """
        Called when market resolves.
        outcome=True means YES tokens pay $1.
        """
        ...
    
    def get_unrealized_pnl(self, market_prices: dict[str, Decimal]) -> Decimal:
        ...
```

#### 2.5 Fill Simulator

```python
# execution/fill_simulator.py

class FillSimulator:
    """
    Simulates order execution against L2 book.
    """
    
    def simulate_fill(
        self,
        signal: Signal,
        book: OrderBook,
        fee_model: FeeModel,
    ) -> Optional[Fill]:
        """
        Walk the book and compute fill.
        Returns None if can't fill (insufficient liquidity or slippage exceeded).
        """
        side = "buy" if signal.action in ("buy_yes", "buy_no") else "sell"
        target_size = Decimal(signal.size)
        
        avg_price, filled = book.walk_book(side, target_size)
        
        if filled < target_size * Decimal("0.9"):  # 90% fill minimum
            return None
            
        slippage_bps = self._calculate_slippage(avg_price, book.mid)
        if slippage_bps > signal.max_slippage_bps:
            return None
            
        return Fill(
            price=avg_price,
            size=int(filled),
            slippage_bps=slippage_bps,
            timestamp_ns=time.time_ns(),
        )
```

**Deliverables:**
- [ ] Clock abstraction with RealTime and Simulated implementations
- [ ] ReplaySource with speed control
- [ ] StrategyRunner with full execution loop
- [ ] Portfolio with settlement logic
- [ ] FillSimulator with L2 walking
- [ ] Unit tests for all components

---

### Phase 3: Strategy Implementations (Days 8-10)

**Goal:** Working strategies with benchmarks.

#### 3.1 Base Strategy & Registry

```python
# strategies/base.py (from section 2.2)
# strategies/registry.py

class StrategyRegistry:
    """Dynamic strategy loading."""
    
    @staticmethod
    def load(name: str, config: dict) -> BaseStrategy:
        """Load strategy by name from implementations/."""
        ...
    
    @staticmethod
    def list_available() -> list[str]:
        """List all available strategy names."""
        ...
```

#### 3.2 Spot Momentum Strategy

```python
# strategies/implementations/spot_momentum.py

class SpotMomentumStrategy(BaseStrategy):
    """
    Trade when spot price movement diverges from market probability.
    
    Logic:
    1. Calculate spot return from window start
    2. Map return to implied probability via logistic function
    3. Compare to market price - trade if edge > threshold
    4. Size using Kelly criterion
    """
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.min_edge_bps = config.get("min_edge_bps", 50)
        self.max_spread_bps = config.get("max_spread_bps", 100)
        self.kelly_fraction = config.get("kelly_fraction", 0.25)
        
    def evaluate(self, state: MarketState, context: StrategyContext) -> Signal:
        # Check spread
        if state.spread_bps > self.max_spread_bps:
            return Signal(action="hold", size=0, rationale="Spread too wide")
        
        # Market-implied probability
        p_market = (state.best_bid + state.best_ask) / 2
        
        # Map spot return to probability using logistic
        # +1% spot return -> ~60% probability
        # -1% spot return -> ~40% probability
        spot_return = state.spot_return_bps / 10000  # Convert bps to decimal
        p_hat = Decimal("0.5") + Decimal("0.1") * self._sigmoid(spot_return * 10)
        
        # Edge calculation
        ev = p_hat - p_market
        ev_bps = int(ev * 10000)
        
        if ev_bps < self.min_edge_bps:
            return Signal(
                action="hold", 
                size=0, 
                rationale=f"No edge: {ev_bps} bps < {self.min_edge_bps}"
            )
        
        # Kelly sizing
        f = self._kelly_fraction(p_hat, p_market)
        bankroll = context.portfolio.cash
        size = int(f * bankroll / p_market)
        
        return Signal(
            action="buy_yes",
            size=max(1, size),
            max_slippage_bps=50,
            rationale=f"Spot momentum: {spot_return:.2%} -> p={p_hat:.2%}, edge={ev_bps} bps",
            p_hat=p_hat,
            ev_bps=ev_bps,
            size_basis="kelly"
        )
    
    def _sigmoid(self, x: float) -> Decimal:
        return Decimal("1") / (Decimal("1") + Decimal(str(math.exp(-x))))
    
    def _kelly_fraction(self, p_hat: Decimal, p_market: Decimal) -> Decimal:
        ev = p_hat - p_market
        if ev <= 0:
            return Decimal("0")
        f_star = ev / (p_hat * (Decimal("1") - p_hat))
        return f_star * Decimal(str(self.kelly_fraction))
```

#### 3.3 Benchmark Strategies

```python
# strategies/implementations/buy_and_hold.py

class BuyAndHoldBenchmark(BaseStrategy):
    """
    Buy YES at market open, hold to expiry.
    Used as benchmark for comparison.
    """
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.markets_entered: set[str] = set()
        
    def evaluate(self, state: MarketState, context: StrategyContext) -> Signal:
        # Only enter once per market, early in window
        if state.condition_id in self.markets_entered:
            return Signal(action="hold", size=0)
        
        if state.elapsed_sec < 60:  # First minute only
            self.markets_entered.add(state.condition_id)
            return Signal(
                action="buy_yes",
                size=10,  # Fixed size for benchmark
                exit_policy="hold_to_expiry",
                rationale="Buy and hold benchmark"
            )
        
        return Signal(action="hold", size=0)
```

#### 3.4 Configuration System

```yaml
# config/strategies/spot_momentum.yaml

name: "Spot Momentum"
version: "1.0"
description: "Trade based on spot price divergence from market probability"

parameters:
  min_edge_bps:
    default: 50
    min: 10
    max: 500
    description: "Minimum edge in basis points to trade"
  
  max_spread_bps:
    default: 100
    min: 10
    max: 500
    description: "Don't trade if spread exceeds this"
  
  kelly_fraction:
    default: 0.25
    min: 0.01
    max: 1.0
    description: "Fraction of full Kelly to use"

risk:
  max_position_per_market: 100
  max_daily_trades: 50
```

**Deliverables:**
- [ ] StrategyRegistry with dynamic loading
- [ ] SpotMomentumStrategy with Bayesian edge detection
- [ ] BuyAndHold benchmark
- [ ] Random baseline strategy
- [ ] YAML configuration system
- [ ] Strategy state serialization

---

### Phase 4: Analysis & Metrics (Days 11-12)

**Goal:** Comprehensive performance reporting.

#### 4.1 Metrics Calculation

```python
# analysis/metrics.py

import numpy as np
from typing import list
from decimal import Decimal

def calculate_sharpe(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """Annualized Sharpe ratio from return series."""
    if len(returns) < 2:
        return 0.0
    excess_returns = [r - risk_free_rate for r in returns]
    return np.mean(excess_returns) / (np.std(excess_returns) + 1e-10) * np.sqrt(252)

def calculate_max_drawdown(equity_curve: list[Decimal]) -> tuple[float, int, int]:
    """
    Returns (max_drawdown_pct, start_idx, end_idx).
    """
    peak = equity_curve[0]
    max_dd = 0.0
    peak_idx = 0
    dd_start = dd_end = 0
    
    for i, value in enumerate(equity_curve):
        if value > peak:
            peak = value
            peak_idx = i
        dd = (peak - value) / peak
        if dd > max_dd:
            max_dd = dd
            dd_start = peak_idx
            dd_end = i
            
    return max_dd, dd_start, dd_end

def calculate_calmar(returns: list[float], max_drawdown: float) -> float:
    """Calmar ratio = annualized return / max drawdown."""
    if max_drawdown == 0:
        return 0.0
    annual_return = np.mean(returns) * 252
    return annual_return / max_drawdown
```

#### 4.2 Report Generation

```python
# analysis/reporting.py

@dataclass
class BacktestReport:
    """Complete backtest analysis."""
    
    # Overview
    run_id: str
    strategy_name: str
    strategy_config: dict
    data_range: tuple[datetime, datetime]
    
    # Returns
    total_return_pct: float
    annualized_return_pct: float
    volatility_pct: float
    
    # Risk
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    calmar_ratio: float
    
    # Trade stats
    total_trades: int
    win_rate: float
    avg_trade_return_bps: float
    profit_factor: float
    avg_win_bps: float
    avg_loss_bps: float
    
    # Benchmark comparison
    vs_buy_hold_excess_return: float
    vs_random_baseline_p_value: float
    
    # Data
    equity_curve: list[tuple[datetime, Decimal]]
    trade_log: list[Trade]
    monthly_returns: list[tuple[str, float]]
    
    def to_html(self) -> str:
        """Generate HTML report."""
        ...
    
    def to_json(self) -> str:
        """Generate JSON for API consumption."""
        ...
    
    def save(self, path: str) -> None:
        """Save report to directory."""
        ...
```

**Deliverables:**
- [ ] Sharpe, Sortino, Calmar, max drawdown calculations
- [ ] Benchmark comparison framework
- [ ] HTML report generation
- [ ] Trade log analysis

---

### Phase 5: CLI & UI Integration (Days 13-14)

**Goal:** User-facing tools.

#### 5.1 Backtest CLI

```bash
# Run backtest
python -m cli.backtest run \
    --strategy spot_momentum \
    --config config/strategies/spot_momentum.yaml \
    --data data_store/events/20250309_*.jsonl \
    --start 2025-03-09T12:00:00 \
    --end 2025-03-09T18:00:00 \
    --initial-cash 10000 \
    --output results/run_001/

# List past runs
python -m cli.backtest list --limit 10

# Analyze specific run
python -m cli.backtest analyze run_001 --format html --open

# Compare runs
python -m cli.backtest compare run_001 run_002 --metric sharpe
```

#### 5.2 Paper Trading CLI

```bash
# Start paper trading
python -m cli.paper start \
    --strategy spot_momentum \
    --initial-cash 10000 \
    --duration 3600  # Run for 1 hour

# Check status
python -m cli.paper status

# View live PnL
python -m cli.paper watch  # Tail the equity curve

# Stop
python -m cli.paper stop
```

#### 5.3 API Endpoints for UI

```python
# app.py additions

@app.get("/api/paper/status")
async def get_paper_status():
    """Get current paper trading session status."""
    ...

@app.get("/api/backtest/runs")
async def list_backtest_runs(limit: int = 10):
    """List recent backtest runs."""
    ...

@app.get("/api/backtest/runs/{run_id}")
async def get_backtest_detail(run_id: str):
    """Get full details of a backtest run."""
    ...

@app.websocket("/ws/paper")
async def paper_websocket(ws: WebSocket):
    """Live updates during paper trading."""
    ...
```

**Deliverables:**
- [ ] Complete CLI interface
- [ ] REST API for UI
- [ ] WebSocket for live updates
- [ ] Integration with existing paper.html

---

## 4. Testing Strategy

### 4.1 Unit Tests

```python
# tests/test_fill_simulator.py

class TestFillSimulator:
    def test_walk_book_basic(self):
        book = OrderBook(
            asset_id="abc",
            timestamp_ns=0,
            bids=[(Decimal("0.45"), Decimal("100")), (Decimal("0.44"), Decimal("200"))],
            asks=[(Decimal("0.50"), Decimal("100")), (Decimal("0.51"), Decimal("200"))],
        )
        
        avg, filled = book.walk_book("buy", Decimal("150"))
        # Should take 100 @ 0.50, 50 @ 0.51 = avg 0.5033...
        assert filled == Decimal("150")
        assert 0.503 < float(avg) < 0.504
    
    def test_slippage_rejection(self):
        # Test that large orders get rejected due to slippage
        ...

class TestKellySizing:
    def test_kelly_zero_edge(self):
        assert kelly_fraction(0.5, 0.5) == 0.0
    
    def test_kelly_full_fraction(self):
        # Known case: p=0.6, market=0.5
        # f* = (0.1) / (0.6*0.4) = 0.4167
        result = kelly_fraction(0.6, 0.5, fraction=1.0)
        assert 0.41 < result < 0.42
```

### 4.2 Integration Tests

```python
# tests/test_backtest_integration.py

class TestBacktestIntegration:
    """
    Test full backtest pipeline with recorded data.
    """
    
    async def test_buy_and_hold_backtest(self):
        """
        Run buy-and-hold strategy on known data.
        Verify final PnL matches expected.
        """
        ...
    
    async def test_spot_momentum_edge_cases(self):
        """
        Test strategy with:
        - Empty order book
        - Extreme volatility
        - Market gaps
        """
        ...
```

### 4.3 Property-Based Tests

```python
# tests/test_properties.py

from hypothesis import given, strategies as st

@given(
    p_hat=st.decimals(min_value=0.01, max_value=0.99),
    p_market=st.decimals(min_value=0.01, max_value=0.99),
)
def test_kelly_never_exceeds_one(p_hat, p_market):
    """Kelly fraction should never exceed 1 (full bankroll)."""
    f = kelly_fraction(float(p_hat), float(p_market))
    assert 0 <= f <= 1
```

---

## 5. Configuration Reference

### 5.1 Environment Variables

```bash
# Data
DATA_STORE_PATH="./data_store"
EVENT_RETENTION_DAYS=30
COMPRESS_AFTER_HOURS=24

# Trading
INITIAL_CASH=10000
DEFAULT_KELLY_FRACTION=0.25
MAX_POSITION_PER_MARKET=1000

# Risk
MAX_DRAWDOWN_PCT=0.10
CIRCUIT_BREAKER_ENABLED=true

# Performance
REPLAY_SPEED_DEFAULT=1.0
MAX_REPLAY_SPEED=1000.0
```

### 5.2 Strategy Configuration Schema

```python
# config/schemas.py

from pydantic import BaseModel, Field

class StrategyConfig(BaseModel):
    name: str
    version: str = "1.0"
    class_path: str  # Python path to strategy class
    
    parameters: dict[str, ParameterConfig]
    risk_limits: RiskLimits
    
class ParameterConfig(BaseModel):
    type: Literal["int", "float", "bool", "choice"]
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[list] = None
    description: str
```

---

## 6. Migration from Current State

### Step 1: Add Recording (Non-Breaking)

```python
# app.py modifications

# Add to imports
from data.recorder import EventRecorder
from data.live_source import LiveEventAdapter

# In lifespan
recorder = EventRecorder()
adapter = LiveEventAdapter(recorder)

# Wire callbacks through adapter
price_engine._on_price = adapter.on_price
market_engine._on_market_event = adapter.on_market_event

# Recorder runs in background
await recorder.start()
```

### Step 2: Parallel Development

- Existing dashboard continues to work unchanged
- New execution modules developed in parallel
- No disruption to live feed

### Step 3: Gradual Integration

- Add `/api/backtest/*` routes
- Add Strategy Lab UI components
- Eventually deprecate direct engine access in favor of unified pipeline

---

## 7. Success Criteria

### Phase 1 Success
- [ ] 24+ hours of continuous recording without data loss
- [ ] Event files < 100MB/hour compressed
- [ ] Replay produces identical event sequence

### Phase 2 Success
- [ ] Backtest completes 1 week of data in < 5 minutes at 100x speed
- [ ] Fill simulation accuracy within 5% of observed fills
- [ ] 100% reproducibility (same seed → same results)

### Phase 3 Success
- [ ] 3+ benchmark strategies implemented
- [ ] Spot momentum shows positive edge vs buy-and-hold
- [ ] Strategy hot-reload works without restart

### Phase 4 Success
- [ ] Sharpe ratio calculation matches external tools (Excel, Python quant libs)
- [ ] HTML reports generated in < 1 second
- [ ] Benchmark comparison statistically valid

### Phase 5 Success
- [ ] CLI covers all common operations
- [ ] WebSocket delivers < 100ms latency updates
- [ ] UI shows live PnL during paper trading

---

## 8. Open Questions

1. **Settlement data source**: How do we get resolution outcomes? Polymarket API or manual input?

2. **Historical spot data**: Do we record Coinbase ticks or use CCXT for backfill?

3. **Multi-strategy**: Run multiple strategies concurrently or sequentially?

4. **Optimization framework**: Grid search, Bayesian optimization, or genetic algorithms?

5. **Live trading**: Is real money trading in scope or paper-only?

---

## Appendix A: File Size Estimates

| Data Type | Frequency | Size/Event | Hourly | Daily |
|-----------|-----------|------------|--------|-------|
| Spot ticks | ~1/sec | 100 bytes | 360 KB | 8.6 MB |
| CLOB best bid/ask | ~10/sec | 200 bytes | 7.2 MB | 173 MB |
| CLOB book snapshots | 0.2/sec | 5 KB | 3.6 MB | 86 MB |
| **Total** | | | **~11 MB/hr** | **~270 MB/day** |

With compression: ~30-50 MB/day (easily manageable)

---

## Appendix B: Glossary

- **CLOB**: Central Limit Order Book (Polymarket's order book system)
- **Kelly**: Kelly criterion for optimal bet sizing
- **L2**: Level 2 market data (full order book)
- **Bps**: Basis points (1/100 of 1%, i.e., 0.01%)
- **Edge**: Expected value advantage over market price
- **Slippage**: Difference between expected and actual fill price
- **Market State**: Immutable snapshot of market conditions at a point in time

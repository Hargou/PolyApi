"""
Server-side paper trading session.

Runs a strategy against live data from PriceEngine + MarketEngine,
pushes state updates to browser clients via a Feed-like broadcaster.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

from strategies.spot_momentum import SpotMomentumStrategy
from strategies.benchmarks import AlwaysYesStrategy, AlwaysNoStrategy, RandomStrategy
from strategies.quant_models import QuantModelsStrategy
from strategies.fee_extremes import FeeExtremesStrategy
from strategies.time_decay import TimeDecayStrategy
from strategies.orderbook_imbalance import OrderBookImbalanceStrategy
from strategies.volatility_regime import VolatilityRegimeStrategy
from strategies.liquidity_vacuum import LiquidityVacuumStrategy
from strategies.consensus import ConsensusStrategy
from strategies.base import BaseStrategy
from execution.runner import StrategyRunner
from execution.portfolio import Portfolio
from execution.risk_engine import RiskConfig
from data.live_source import LiveSource
from data.models import Event
from analysis.metrics import compute_metrics

log = logging.getLogger(__name__)

STRATEGIES = {
    "quant_models": lambda: QuantModelsStrategy(),
    "fee_extremes": lambda: FeeExtremesStrategy(),
    "time_decay": lambda: TimeDecayStrategy(),
    "orderbook_imbalance": lambda: OrderBookImbalanceStrategy(),
    "volatility_regime": lambda: VolatilityRegimeStrategy(),
    "liquidity_vacuum": lambda: LiquidityVacuumStrategy(),
    "consensus": lambda: ConsensusStrategy(),
    "spot_momentum": lambda: SpotMomentumStrategy(),
    "always_yes": lambda: AlwaysYesStrategy(),
    "always_no": lambda: AlwaysNoStrategy(),
    "random": lambda: RandomStrategy(),
}


class PaperSession:
    """
    Manages a paper trading session that runs server-side.

    Hooks into the existing PriceEngine + MarketEngine, runs a StrategyRunner,
    and broadcasts state to connected WebSocket clients.
    """

    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Session state
        self.strategy_name: str = ""
        self.portfolio: Optional[Portfolio] = None
        self.runner: Optional[StrategyRunner] = None
        self.source: Optional[LiveSource] = None
        self.start_time: float = 0
        self.event_count: int = 0
        self.last_trade_count: int = 0
        self.last_settled_count: int = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def connect(self, ws: WebSocket):
        """Accept a browser WebSocket and send current state."""
        await ws.accept()
        self._clients.add(ws)
        await self._send_to(ws, self._build_snapshot())

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    async def start(self, strategy_name: str, bankroll: float,
                    book_poll: float, price_engine, market_engine):
        """Start a paper trading session."""
        if self._running:
            await self.stop()

        if strategy_name not in STRATEGIES:
            return {"error": f"Unknown strategy: {strategy_name}"}

        self.strategy_name = strategy_name
        self.portfolio = Portfolio(bankroll=bankroll)
        strategy = STRATEGIES[strategy_name]()
        self.runner = StrategyRunner(strategy, self.portfolio, RiskConfig())
        self.source = LiveSource(book_poll_interval=book_poll)
        self.source.install(price_engine, market_engine)
        self.start_time = time.time()
        self.event_count = 0
        self.last_trade_count = 0
        self.last_settled_count = 0

        await self.source.start()
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

        await self._broadcast({"type": "status", "status": "running",
                               "strategy": strategy_name, "bankroll": bankroll})
        return {"status": "started", "strategy": strategy_name}

    async def stop(self):
        """Stop the session."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.source:
            await self.source.stop()
            self.source = None

        snapshot = self._build_snapshot()
        snapshot["type"] = "stopped"
        await self._broadcast(snapshot)

    async def _run_loop(self):
        """Main event loop: consume events, run strategy, broadcast updates."""
        try:
            async for event in self.source.events():
                if not self._running:
                    break

                self.runner._process_event(event)
                self.event_count += 1

                # Broadcast on new trades, settlements, or every 50 events
                new_trades = len(self.portfolio.trades) > self.last_trade_count
                new_settled = len(self.portfolio.settled) > self.last_settled_count
                periodic = self.event_count % 50 == 0

                if new_trades or new_settled or periodic:
                    # Build incremental update
                    update = self._build_update(new_trades, new_settled)
                    await self._broadcast(update)
                    self.last_trade_count = len(self.portfolio.trades)
                    self.last_settled_count = len(self.portfolio.settled)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Paper session error")

    def _build_snapshot(self) -> dict:
        """Full state snapshot for new connections."""
        if not self.portfolio:
            return {"type": "snapshot", "status": "idle"}

        metrics = compute_metrics(self.portfolio) if self.portfolio.settled else {}
        elapsed = time.time() - self.start_time if self.start_time else 0

        trades = []
        for t in self.portfolio.trades[-50:]:  # last 50 trades
            trades.append({
                "ts": t.ts, "asset": t.asset, "side": t.side,
                "size": t.size, "fill_price": round(t.fill_price, 4),
                "slippage_bps": round(t.slippage_bps, 1),
                "fee": round(t.fee, 4), "rationale": t.rationale,
            })

        settlements = []
        for s in self.portfolio.settled[-50:]:
            settlements.append({
                "asset": s.position.asset, "side": s.position.side,
                "size": s.position.size,
                "entry_price": round(s.position.entry_price, 4),
                "outcome": s.outcome, "pnl": round(s.pnl, 2),
                "settled_ts": s.settled_ts,
            })

        positions = []
        for cid, p in self.portfolio.positions.items():
            positions.append({
                "condition_id": cid, "asset": p.asset, "side": p.side,
                "size": p.size, "entry_price": round(p.entry_price, 4),
                "entry_fee": round(p.entry_fee, 4),
                "window_end_ts": p.window_end_ts,
            })

        return {
            "type": "snapshot",
            "status": "running" if self._running else "stopped",
            "strategy": self.strategy_name,
            "elapsed_sec": round(elapsed, 1),
            "event_count": self.event_count,
            "bankroll": round(self.portfolio.bankroll, 2),
            "initial_bankroll": self.portfolio.initial_bankroll,
            "net_pnl": round(self.portfolio.net_pnl, 2),
            "total_fees": round(self.portfolio.total_fees, 4),
            "positions": positions,
            "trades": trades,
            "settlements": settlements,
            "metrics": metrics,
            "summary": self.portfolio.summary(),
        }

    def _build_update(self, new_trades: bool, new_settled: bool) -> dict:
        """Incremental update with summary + new activity."""
        elapsed = time.time() - self.start_time if self.start_time else 0

        update: Dict[str, Any] = {
            "type": "update",
            "elapsed_sec": round(elapsed, 1),
            "event_count": self.event_count,
            "bankroll": round(self.portfolio.bankroll, 2),
            "net_pnl": round(self.portfolio.net_pnl, 2),
            "total_fees": round(self.portfolio.total_fees, 4),
            "summary": self.portfolio.summary(),
        }

        # Include open positions
        positions = []
        for cid, p in self.portfolio.positions.items():
            positions.append({
                "condition_id": cid, "asset": p.asset, "side": p.side,
                "size": p.size, "entry_price": round(p.entry_price, 4),
                "window_end_ts": p.window_end_ts,
            })
        update["positions"] = positions

        if new_trades:
            t = self.portfolio.trades[-1]
            update["new_trade"] = {
                "ts": t.ts, "asset": t.asset, "side": t.side,
                "size": t.size, "fill_price": round(t.fill_price, 4),
                "slippage_bps": round(t.slippage_bps, 1),
                "fee": round(t.fee, 4), "rationale": t.rationale,
            }

        if new_settled:
            s = self.portfolio.settled[-1]
            update["new_settlement"] = {
                "asset": s.position.asset, "side": s.position.side,
                "size": s.position.size, "outcome": s.outcome,
                "pnl": round(s.pnl, 2), "settled_ts": s.settled_ts,
            }

            # Include updated metrics after settlement
            update["metrics"] = compute_metrics(self.portfolio)

        return update

    async def _broadcast(self, message: dict):
        """Send to all connected paper trading clients."""
        if not self._clients:
            return
        data = json.dumps(message)
        dead = []
        for ws in self._clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def _send_to(self, ws: WebSocket, message: dict):
        try:
            await ws.send_json(message)
        except Exception:
            pass

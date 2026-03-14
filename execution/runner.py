"""
StrategyRunner: the event loop that drives strategies.
Processes events sequentially, builds MarketState, calls strategy, routes to OrderManager.
Works identically for backtest (replay) and paper trading (live).
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

from strategies.base import BaseStrategy, MarketState, Signal, ExitPolicy
from execution.portfolio import Portfolio
from execution.risk_engine import RiskEngine, RiskConfig
from execution.order_manager import OrderManager
from data.models import Event, SpotTick, ClobSnapshot, MarketInfo, MarketResolution

log = logging.getLogger(__name__)


class StrategyRunner:
    """
    Event-driven strategy execution engine.
    Processes a stream of Events, builds MarketState, and routes signals.
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        portfolio: Portfolio,
        risk_config: RiskConfig = None,
    ):
        self.strategy = strategy
        self.portfolio = portfolio
        self.risk = RiskEngine(risk_config or RiskConfig())
        self.order_manager = OrderManager(self.risk, self.portfolio)

        # State tracking
        self._spot_prices: Dict[str, float] = {}          # symbol -> latest price
        self._spot_at_window_start: Dict[str, float] = {} # condition_id -> spot price at start
        self._markets: Dict[str, MarketInfo] = {}          # condition_id -> MarketInfo
        self._books: Dict[str, ClobSnapshot] = {}          # condition_id -> latest book
        self._processed_markets: set = set()               # markets we already traded

    def run(self, events: List[Event]) -> dict:
        """
        Run the strategy over a list of events (backtest mode).

        Args:
            events: time-sorted list of Events

        Returns:
            Summary dict with portfolio and order manager stats.
        """
        log.info("Starting backtest: %d events, strategy=%s", len(events), self.strategy.name)
        start = time.time()

        for event in events:
            self._process_event(event)

        elapsed = time.time() - start
        summary = self._build_summary(elapsed)
        log.info("Backtest complete: %.1fs, %d trades, net_pnl=$%.2f",
                 elapsed, len(self.portfolio.trades), self.portfolio.net_pnl)
        return summary

    def _process_event(self, event: Event):
        """Route an event to the appropriate handler."""
        if event.type == "spot" and event.spot:
            self._handle_spot(event.spot)

        elif event.type == "clob" and event.clob:
            self._handle_clob(event.clob)

        elif event.type == "market_info" and event.market_info:
            self._handle_market_info(event.market_info)

        elif event.type == "resolution" and event.resolution:
            self._handle_resolution(event.resolution)

    def _handle_spot(self, tick: SpotTick):
        """Update spot price state."""
        self._spot_prices[tick.symbol] = tick.price

    def _handle_market_info(self, info: MarketInfo):
        """Register a new market and snapshot the spot price at window start."""
        self._markets[info.condition_id] = info
        sym = f"{info.asset.lower()}usdt"
        if sym in self._spot_prices:
            self._spot_at_window_start[info.condition_id] = self._spot_prices[sym]
        # Snapshot all spot prices for cross-asset correlation
        for s, p in self._spot_prices.items():
            self._spot_at_window_start[f"_global_{s}"] = p

    def _handle_clob(self, snap: ClobSnapshot):
        """Process a CLOB snapshot: update book, evaluate strategy if we have a market."""
        self._books[snap.condition_id] = snap

        market = self._markets.get(snap.condition_id)
        if not market:
            return

        # Don't re-evaluate markets we already traded
        if snap.condition_id in self._processed_markets:
            # But check for dynamic exit
            if (self.strategy.exit_policy == ExitPolicy.DYNAMIC
                    and snap.condition_id in self.portfolio.positions):
                state = self._build_state(market, snap)
                if state and self.strategy.should_exit(state, self.portfolio.positions[snap.condition_id]):
                    self._execute_exit(snap.condition_id, snap)
            return

        state = self._build_state(market, snap)
        if not state:
            return

        # Evaluate strategy
        signal = self.strategy.evaluate(state)

        if signal.action != "hold":
            result = self.order_manager.process_signal(
                signal=signal,
                state=state,
                bids=snap.bids,
                asks=snap.asks,
            )
            if result.position:
                self._processed_markets.add(snap.condition_id)

                # Record loss for risk engine cooldown
                # (we don't know PnL yet, but we track the trade)

    def _handle_resolution(self, res: MarketResolution):
        """Settle any open position in the resolved market."""
        settled = self.portfolio.settle(res.condition_id, res.outcome, res.ts)
        if settled:
            log.info("SETTLED %s: outcome=%s, pnl=$%.2f",
                     settled.position.asset, res.outcome, settled.pnl)
            self.strategy.on_market_resolved(res.condition_id, res.outcome, settled.pnl)

            if settled.pnl < 0:
                self.risk.record_loss(res.ts)

        # Clean up state for this market
        self._processed_markets.discard(res.condition_id)
        self._markets.pop(res.condition_id, None)
        self._books.pop(res.condition_id, None)
        self._spot_at_window_start.pop(res.condition_id, None)

    def _execute_exit(self, condition_id: str, snap: ClobSnapshot):
        """Execute a dynamic exit by selling the position."""
        pos = self.portfolio.positions.get(condition_id)
        if not pos:
            return

        from execution.fill_simulator import simulate_fill
        from execution.fees import taker_fee

        # Sell: if we hold YES, hit bids; if we hold NO, lift asks
        if pos.side == "yes":
            fill = simulate_fill("buy_no", pos.size, snap.bids, snap.asks)
        else:
            fill = simulate_fill("buy_yes", pos.size, snap.bids, snap.asks)

        if fill.filled:
            pnl = self.portfolio.close_position(
                condition_id, fill.avg_price, fill.fee, snap.ts, fill.slippage_bps
            )
            if pnl is not None:
                log.info("DYNAMIC EXIT %s: pnl=$%.2f", pos.asset, pnl)
                if pnl < 0:
                    self.risk.record_loss(snap.ts)

    def _build_state(self, market: MarketInfo, snap: ClobSnapshot) -> Optional[MarketState]:
        """Build a MarketState from current data. Returns None if insufficient data."""
        sym = f"{market.asset.lower()}usdt"
        spot = self._spot_prices.get(sym)
        if spot is None:
            return None

        spot_start = self._spot_at_window_start.get(market.condition_id, spot)
        now_sec = snap.ts // 1000
        elapsed = max(0, now_sec - market.window_start_ts)
        remaining = max(0, market.window_end_ts - now_sec)

        midpoint = (snap.best_bid + snap.best_ask) / 2 if (snap.best_bid + snap.best_ask) > 0 else 0.5
        spread = snap.best_ask - snap.best_bid
        spread_bps = (spread / midpoint * 10_000) if midpoint > 0 else 0.0

        bid_depth = sum(p * s for p, s in snap.bids)
        ask_depth = sum(p * s for p, s in snap.asks)

        spot_return = ((spot - spot_start) / spot_start * 10_000) if spot_start > 0 else 0.0

        # Microstructure: best-level sizes, microprice, OBI
        bid_size_best = float(snap.bids[0][1]) if snap.bids else 0.0
        ask_size_best = float(snap.asks[0][1]) if snap.asks else 0.0

        if bid_size_best + ask_size_best > 0:
            microprice = (snap.best_bid * ask_size_best + snap.best_ask * bid_size_best) / (bid_size_best + ask_size_best)
        else:
            microprice = midpoint

        total_depth = bid_depth + ask_depth
        obi = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

        # Cross-asset spot returns
        other_returns = {}
        for other_sym, other_spot in self._spot_prices.items():
            other_asset = other_sym.replace("usdt", "").upper()
            if other_asset != market.asset:
                other_start = self._spot_at_window_start.get(
                    f"_global_{other_sym}", other_spot)
                if other_start > 0:
                    other_returns[other_asset] = ((other_spot - other_start) / other_start * 10_000)

        # Effective fee rate at midpoint
        eff_fee = 0.25 * (midpoint * (1.0 - midpoint)) ** 2 if 0 < midpoint < 1 else 0.0

        return MarketState(
            condition_id=market.condition_id,
            yes_token_id=market.yes_token_id,
            asset=market.asset,
            slug=market.slug,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
            spread=spread,
            spread_bps=spread_bps,
            midpoint=midpoint,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            spot_price=spot,
            spot_price_at_window_start=spot_start,
            spot_return_bps=spot_return,
            window_start_ts=market.window_start_ts,
            window_end_ts=market.window_end_ts,
            elapsed_sec=elapsed,
            remaining_sec=remaining,
            ts=snap.ts,
            bids=snap.bids,
            asks=snap.asks,
            bid_size_at_best=bid_size_best,
            ask_size_at_best=ask_size_best,
            microprice=microprice,
            obi=obi,
            other_spot_returns=other_returns,
            effective_fee_rate=eff_fee,
        )

    def _build_summary(self, elapsed_sec: float) -> dict:
        """Build a summary of the run."""
        return {
            "strategy": self.strategy.name,
            "elapsed_sec": round(elapsed_sec, 2),
            "portfolio": self.portfolio.summary(),
            "orders": self.order_manager.summary(),
            "risk_config": {
                k: v for k, v in self.risk.config.__dict__.items()
            },
        }

    def reset(self):
        """Reset all state for a new run."""
        self.portfolio.reset()
        self.order_manager.reset()
        self.risk.reset()
        self.strategy.reset()
        self._spot_prices.clear()
        self._spot_at_window_start.clear()
        self._markets.clear()
        self._books.clear()
        self._processed_markets.clear()

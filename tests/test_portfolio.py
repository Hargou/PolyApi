"""Tests for portfolio position tracking, settlement, and PnL."""

import pytest
from execution.portfolio import Portfolio


class TestPortfolioOpen:
    def test_open_position_deducts_cost(self):
        p = Portfolio(bankroll=1000.0)
        p.open_position(
            condition_id="c1", yes_token_id="t1", asset="BTC", slug="btc-1",
            side="yes", size=100, fill_price=0.50, fee=0.50, ts=1000,
            window_end_ts=2000,
        )
        # cost = 100 * 0.50 + 0.50 = 50.50
        assert abs(p.bankroll - 949.50) < 1e-10

    def test_open_position_tracked(self):
        p = Portfolio(bankroll=1000.0)
        pos = p.open_position(
            condition_id="c1", yes_token_id="t1", asset="BTC", slug="btc-1",
            side="yes", size=100, fill_price=0.50, fee=0.50, ts=1000,
            window_end_ts=2000,
        )
        assert "c1" in p.positions
        assert p.open_position_count == 1
        assert pos.side == "yes"
        assert pos.size == 100

    def test_trade_recorded(self):
        p = Portfolio(bankroll=1000.0)
        p.open_position(
            condition_id="c1", yes_token_id="t1", asset="BTC", slug="btc-1",
            side="no", size=50, fill_price=0.40, fee=0.10, ts=1000,
            window_end_ts=2000, rationale="test",
        )
        assert len(p.trades) == 1
        assert p.trades[0].side == "buy_no"
        assert p.trades[0].rationale == "test"

    def test_fees_accumulated(self):
        p = Portfolio(bankroll=1000.0)
        p.open_position(
            condition_id="c1", yes_token_id="t1", asset="BTC", slug="btc-1",
            side="yes", size=100, fill_price=0.50, fee=1.0, ts=1000,
            window_end_ts=2000,
        )
        p.open_position(
            condition_id="c2", yes_token_id="t2", asset="ETH", slug="eth-1",
            side="no", size=50, fill_price=0.60, fee=0.50, ts=1001,
            window_end_ts=2000,
        )
        assert abs(p.total_fees - 1.50) < 1e-10


class TestPortfolioSettle:
    def _open_yes(self, p, cid="c1", price=0.50, size=100, fee=0.50):
        p.open_position(
            condition_id=cid, yes_token_id="t1", asset="BTC", slug="btc-1",
            side="yes", size=size, fill_price=price, fee=fee, ts=1000,
            window_end_ts=2000,
        )

    def test_win_payout(self):
        """Winning YES position: payout = size * 1.0."""
        p = Portfolio(bankroll=1000.0)
        self._open_yes(p, price=0.50, size=100, fee=0.50)
        # cost = 50.50, bankroll = 949.50
        settled = p.settle("c1", "yes", ts=2000)
        assert settled is not None
        assert settled.payout == 100.0
        assert abs(settled.pnl - (100.0 - 50.50)) < 1e-10
        assert abs(p.bankroll - (949.50 + 100.0)) < 1e-10

    def test_loss_payout(self):
        """Losing YES position: payout = 0."""
        p = Portfolio(bankroll=1000.0)
        self._open_yes(p, price=0.50, size=100, fee=0.50)
        settled = p.settle("c1", "no", ts=2000)
        assert settled.payout == 0.0
        assert abs(settled.pnl - (0.0 - 50.50)) < 1e-10

    def test_no_position_returns_none(self):
        p = Portfolio(bankroll=1000.0)
        assert p.settle("nonexistent", "yes", ts=2000) is None

    def test_position_removed_after_settle(self):
        p = Portfolio(bankroll=1000.0)
        self._open_yes(p)
        p.settle("c1", "yes", ts=2000)
        assert "c1" not in p.positions
        assert p.open_position_count == 0

    def test_no_side_wins_when_outcome_no(self):
        """NO position wins when market resolves NO."""
        p = Portfolio(bankroll=1000.0)
        p.open_position(
            condition_id="c1", yes_token_id="t1", asset="BTC", slug="btc-1",
            side="no", size=100, fill_price=0.40, fee=0.30, ts=1000,
            window_end_ts=2000,
        )
        settled = p.settle("c1", "no", ts=2000)
        assert settled.payout == 100.0  # won
        cost = 100 * 0.40 + 0.30
        assert abs(settled.pnl - (100.0 - cost)) < 1e-10


class TestPortfolioClose:
    def test_dynamic_exit(self):
        """Close a position mid-window and compute PnL."""
        p = Portfolio(bankroll=1000.0)
        p.open_position(
            condition_id="c1", yes_token_id="t1", asset="BTC", slug="btc-1",
            side="yes", size=100, fill_price=0.50, fee=0.50, ts=1000,
            window_end_ts=2000,
        )
        # Entry cost = 50.50, bankroll = 949.50
        pnl = p.close_position("c1", fill_price=0.60, fee=0.40, ts=1500)
        # Proceeds = 100 * 0.60 - 0.40 = 59.60
        # PnL = 59.60 - 50.50 = 9.10
        assert pnl is not None
        assert abs(pnl - 9.10) < 1e-10
        assert "c1" not in p.positions

    def test_close_nonexistent_returns_none(self):
        p = Portfolio(bankroll=1000.0)
        assert p.close_position("nope", 0.50, 0.0, 1000) is None

    def test_close_records_trade(self):
        p = Portfolio(bankroll=1000.0)
        p.open_position(
            condition_id="c1", yes_token_id="t1", asset="BTC", slug="btc-1",
            side="yes", size=100, fill_price=0.50, fee=0.50, ts=1000,
            window_end_ts=2000,
        )
        p.close_position("c1", fill_price=0.55, fee=0.30, ts=1500)
        assert len(p.trades) == 2  # entry + exit
        assert p.trades[1].side == "sell_yes"


class TestPortfolioSummary:
    def test_summary_fields(self):
        p = Portfolio(bankroll=1000.0)
        s = p.summary()
        assert s["bankroll"] == 1000.0
        assert s["open_positions"] == 0
        assert s["win_rate"] == 0.0

    def test_win_rate_calculation(self):
        p = Portfolio(bankroll=1000.0)
        # Two trades: one win, one loss
        p.open_position("c1", "t1", "BTC", "btc-1", "yes", 100, 0.50, 0.50, 1000, 2000)
        p.open_position("c2", "t2", "ETH", "eth-1", "yes", 100, 0.50, 0.50, 1000, 2000)
        p.settle("c1", "yes", 2000)  # win
        p.settle("c2", "no", 2000)   # loss
        s = p.summary()
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["win_rate"] == 50.0


class TestPortfolioReset:
    def test_reset_clears_state(self):
        p = Portfolio(bankroll=1000.0)
        p.open_position("c1", "t1", "BTC", "btc-1", "yes", 100, 0.50, 0.50, 1000, 2000)
        p.settle("c1", "yes", 2000)
        p.reset()
        assert p.bankroll == 1000.0
        assert p.open_position_count == 0
        assert len(p.trades) == 0
        assert len(p.settled) == 0
        assert p.total_fees == 0.0

"""Tests for L2 order book fill simulation."""

import pytest
from execution.fill_simulator import walk_book, simulate_fill, book_depth, FillResult


class TestWalkBook:
    def test_single_level_full_fill(self):
        levels = [(0.50, 100.0)]
        avg, filled, unfilled = walk_book(levels, 100)
        assert avg == 0.50
        assert filled == 100.0
        assert unfilled == 0.0

    def test_single_level_partial_fill(self):
        levels = [(0.50, 50.0)]
        avg, filled, unfilled = walk_book(levels, 100)
        assert avg == 0.50
        assert filled == 50.0
        assert unfilled == 50.0

    def test_multiple_levels_vwap(self):
        """Walking two levels produces a volume-weighted average price."""
        levels = [(0.50, 50.0), (0.52, 50.0)]
        avg, filled, unfilled = walk_book(levels, 100)
        expected_avg = (50 * 0.50 + 50 * 0.52) / 100
        assert abs(avg - expected_avg) < 1e-10
        assert filled == 100.0
        assert unfilled == 0.0

    def test_empty_book(self):
        avg, filled, unfilled = walk_book([], 100)
        assert avg == 0.0
        assert filled == 0.0
        assert unfilled == 100.0

    def test_zero_size(self):
        levels = [(0.50, 100.0)]
        avg, filled, unfilled = walk_book(levels, 0)
        assert filled == 0.0
        assert unfilled == 0.0

    def test_three_levels_partial(self):
        """Fill 150 contracts across three levels of 60 each."""
        levels = [(0.48, 60.0), (0.50, 60.0), (0.52, 60.0)]
        avg, filled, unfilled = walk_book(levels, 150)
        expected = (60 * 0.48 + 60 * 0.50 + 30 * 0.52) / 150
        assert abs(avg - expected) < 1e-10
        assert filled == 150.0
        assert unfilled == 0.0


class TestSimulateFill:
    def test_buy_yes_lifts_asks(self):
        asks = [(0.52, 100.0), (0.54, 100.0)]
        bids = [(0.48, 100.0)]
        result = simulate_fill("buy_yes", 50, bids, asks)
        assert result.filled is True
        assert result.avg_price == 0.52  # all filled at best ask
        assert result.filled_size == 50.0
        assert result.slippage_bps == 0.0  # no slippage (single level)

    def test_buy_no_hits_bids(self):
        bids = [(0.48, 100.0), (0.46, 100.0)]
        asks = [(0.52, 100.0)]
        result = simulate_fill("buy_no", 50, bids, asks)
        assert result.filled is True
        assert result.avg_price == 0.48

    def test_slippage_across_levels(self):
        asks = [(0.50, 50.0), (0.52, 50.0)]  # narrow gap to stay under default 500 bps
        bids = []
        result = simulate_fill("buy_yes", 100, bids, asks)
        assert result.filled is True
        expected_avg = (50 * 0.50 + 50 * 0.52) / 100
        assert abs(result.avg_price - expected_avg) < 1e-10
        # Slippage relative to best ask (0.50)
        expected_slip = (expected_avg - 0.50) / 0.50 * 10_000
        assert abs(result.slippage_bps - expected_slip) < 1e-6

    def test_slippage_rejection(self):
        """Rejects fill if slippage exceeds max."""
        asks = [(0.50, 10.0), (0.60, 100.0)]  # huge gap
        result = simulate_fill("buy_yes", 100, [], asks, max_slippage_bps=100)
        assert result.filled is False

    def test_empty_book_no_fill(self):
        result = simulate_fill("buy_yes", 100, [], [])
        assert result.filled is False
        assert result.unfilled_size == 100.0

    def test_zero_size(self):
        result = simulate_fill("buy_yes", 0, [], [(0.50, 100.0)])
        assert result.filled is False

    def test_fee_is_computed(self):
        asks = [(0.50, 100.0)]
        result = simulate_fill("buy_yes", 100, [], asks)
        assert result.filled is True
        assert result.fee > 0
        # Fee should match taker_fee(0.50, 100)
        from execution.fees import taker_fee
        expected_fee = taker_fee(0.50, 100)
        assert abs(result.fee - expected_fee) < 1e-10

    def test_total_cost_correct(self):
        asks = [(0.52, 100.0)]
        result = simulate_fill("buy_yes", 50, [], asks)
        assert result.filled is True
        assert abs(result.total_cost - (50 * 0.52 + result.fee)) < 1e-10

    def test_invalid_side(self):
        result = simulate_fill("sell", 100, [(0.50, 100)], [(0.52, 100)])
        assert result.filled is False


class TestBookDepth:
    def test_depth_calculation(self):
        levels = [(0.50, 100.0), (0.48, 200.0)]
        depth = book_depth(levels)
        expected = 0.50 * 100 + 0.48 * 200
        assert abs(depth - expected) < 1e-10

    def test_empty_book(self):
        assert book_depth([]) == 0.0

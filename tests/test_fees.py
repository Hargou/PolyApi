"""Tests for the Polymarket fee model."""

import pytest
from execution.fees import taker_fee, maker_rebate, effective_rate, round_trip_cost


class TestTakerFee:
    def test_fee_at_50_pct(self):
        """Fee is maximized at 50% probability."""
        fee = taker_fee(0.50, 100)
        # 100 * 0.50 * 0.25 * (0.50 * 0.50)^2 = 100 * 0.50 * 0.25 * 0.0625 = 0.78125
        assert abs(fee - 0.78125) < 1e-6

    def test_fee_at_extremes_near_zero(self):
        """Fee approaches zero at price extremes."""
        fee_5c = taker_fee(0.05, 100)
        fee_95c = taker_fee(0.95, 100)
        assert fee_5c < 0.01
        assert fee_95c < 0.10  # still much smaller than the ~0.78 at 50%

    def test_fee_symmetry(self):
        """Fee at price p equals fee at price (1-p) scaled by the price ratio."""
        # Not perfectly symmetric because of the leading `price` term
        fee_30 = taker_fee(0.30, 100)
        fee_70 = taker_fee(0.70, 100)
        # (p * (1-p))^2 is the same for 0.3 and 0.7, but the leading `price` differs
        # fee_30 / fee_70 should equal 0.30 / 0.70
        assert abs(fee_30 / fee_70 - 0.30 / 0.70) < 1e-6

    def test_fee_scales_with_size(self):
        """Fee scales linearly with size."""
        fee_100 = taker_fee(0.50, 100)
        fee_200 = taker_fee(0.50, 200)
        assert abs(fee_200 - 2 * fee_100) < 1e-10

    def test_zero_size_returns_zero(self):
        assert taker_fee(0.50, 0) == 0.0

    def test_price_at_boundary_returns_zero(self):
        assert taker_fee(0.0, 100) == 0.0
        assert taker_fee(1.0, 100) == 0.0

    def test_negative_size_returns_zero(self):
        assert taker_fee(0.50, -10) == 0.0

    def test_very_small_fee_rounds_to_zero(self):
        """Fees below 0.0001 are rounded to zero."""
        fee = taker_fee(0.01, 1)
        # 1 * 0.01 * 0.25 * (0.01 * 0.99)^2 = very small
        assert fee == 0.0  # below threshold


class TestMakerRebate:
    def test_default_rebate_20_pct(self):
        rebate = maker_rebate(1.0)
        assert rebate == 0.20

    def test_custom_rebate(self):
        rebate = maker_rebate(1.0, rebate_pct=0.50)
        assert rebate == 0.50


class TestEffectiveRate:
    def test_max_at_50_pct(self):
        """Effective rate is maximized at 50%."""
        rate_50 = effective_rate(0.50)
        rate_30 = effective_rate(0.30)
        rate_70 = effective_rate(0.70)
        assert rate_50 > rate_30
        assert rate_50 > rate_70

    def test_boundary_returns_zero(self):
        assert effective_rate(0.0) == 0.0
        assert effective_rate(1.0) == 0.0


class TestRoundTripCost:
    def test_hold_to_expiry_no_exit_fee(self):
        """If exit price is 1.0 (win) or 0.0 (loss), no exit fee."""
        cost_win = round_trip_cost(0.50, 1.0, 100)
        cost_loss = round_trip_cost(0.50, 0.0, 100)
        entry_fee = taker_fee(0.50, 100)
        assert abs(cost_win - entry_fee) < 1e-10
        assert abs(cost_loss - entry_fee) < 1e-10

    def test_mid_window_exit_has_double_fee(self):
        """Exiting mid-window at a non-boundary price incurs both entry and exit fees."""
        cost = round_trip_cost(0.50, 0.60, 100)
        entry = taker_fee(0.50, 100)
        exit_ = taker_fee(0.60, 100)
        assert abs(cost - (entry + exit_)) < 1e-10

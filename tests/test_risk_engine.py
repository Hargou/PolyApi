"""Tests for the risk engine pre-trade checks."""

import pytest
from execution.risk_engine import RiskEngine, RiskConfig
from strategies.base import Signal, MarketState


def _make_state(**overrides) -> MarketState:
    """Helper to create a MarketState with sensible defaults."""
    defaults = dict(
        condition_id="c1",
        yes_token_id="t1",
        asset="BTC",
        slug="btc-updown-5m-1000",
        best_bid=0.48,
        best_ask=0.52,
        spread=0.04,
        spread_bps=200.0,
        midpoint=0.50,
        bid_depth=500.0,
        ask_depth=500.0,
        spot_price=84000.0,
        spot_price_at_window_start=84000.0,
        spot_return_bps=0.0,
        window_start_ts=1000,
        window_end_ts=1300,
        elapsed_sec=60,
        remaining_sec=240,
        ts=1060000,
    )
    defaults.update(overrides)
    return MarketState(**defaults)


def _make_signal(**overrides) -> Signal:
    defaults = dict(action="buy_yes", size=100, max_slippage_bps=200, rationale="test")
    defaults.update(overrides)
    return Signal(**defaults)


class TestRiskEngineSpread:
    def test_blocks_wide_spread(self):
        cfg = RiskConfig(max_spread_bps=500.0)
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=600.0)
        sig = _make_signal()
        allowed, reason = engine.check(sig, state, 0, 0.0, 0.0, 10000.0)
        assert not allowed
        assert "spread" in reason

    def test_allows_tight_spread(self):
        cfg = RiskConfig(max_spread_bps=500.0)
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=200.0)
        sig = _make_signal()
        allowed, _ = engine.check(sig, state, 0, 0.0, 0.0, 10000.0)
        assert allowed


class TestRiskEngineTiming:
    def test_blocks_too_little_remaining(self):
        cfg = RiskConfig(min_remaining_sec=30)
        engine = RiskEngine(cfg)
        state = _make_state(remaining_sec=20)
        sig = _make_signal()
        allowed, reason = engine.check(sig, state, 0, 0.0, 0.0, 10000.0)
        assert not allowed
        assert "remaining" in reason

    def test_blocks_elapsed_too_high(self):
        cfg = RiskConfig(max_elapsed_sec=240)
        engine = RiskEngine(cfg)
        state = _make_state(elapsed_sec=250)
        sig = _make_signal()
        allowed, reason = engine.check(sig, state, 0, 0.0, 0.0, 10000.0)
        assert not allowed
        assert "elapsed" in reason

    def test_allows_good_timing(self):
        cfg = RiskConfig(min_remaining_sec=30, max_elapsed_sec=240)
        engine = RiskEngine(cfg)
        state = _make_state(elapsed_sec=60, remaining_sec=240)
        sig = _make_signal()
        allowed, _ = engine.check(sig, state, 0, 0.0, 0.0, 10000.0)
        assert allowed


class TestRiskEnginePositionLimits:
    def test_blocks_oversized_position(self):
        cfg = RiskConfig(max_position_per_market=100)
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=100)
        sig = _make_signal(size=200)
        allowed, reason = engine.check(sig, state, 0, 0.0, 0.0, 10000.0)
        assert not allowed
        assert "size" in reason

    def test_blocks_too_many_concurrent(self):
        cfg = RiskConfig(max_concurrent_positions=3)
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=100)
        sig = _make_signal()
        allowed, reason = engine.check(sig, state, 3, 0.0, 0.0, 10000.0)
        assert not allowed
        assert "positions" in reason

    def test_blocks_exposure_limit(self):
        cfg = RiskConfig(max_total_exposure=100.0)
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=100, midpoint=0.50)
        sig = _make_signal(size=300)  # notional = 300 * 0.50 = 150 > 100
        allowed, reason = engine.check(sig, state, 0, 0.0, 0.0, 10000.0)
        assert not allowed
        assert "exposure" in reason


class TestRiskEngineDrawdown:
    def test_blocks_on_drawdown(self):
        cfg = RiskConfig(max_drawdown_pct=10.0)
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=100)
        sig = _make_signal()
        # session_pnl = -1500, bankroll = 10000 -> drawdown = 15%
        allowed, reason = engine.check(sig, state, 0, 0.0, -1500.0, 10000.0)
        assert not allowed
        assert "drawdown" in reason

    def test_allows_under_drawdown(self):
        cfg = RiskConfig(max_drawdown_pct=10.0)
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=100)
        sig = _make_signal()
        allowed, _ = engine.check(sig, state, 0, 0.0, -500.0, 10000.0)
        assert allowed


class TestRiskEngineCooldown:
    def test_blocks_during_cooldown(self):
        cfg = RiskConfig(cooldown_after_loss_sec=60)
        engine = RiskEngine(cfg)
        engine.record_loss(1000000)  # loss at ts=1000000 ms
        state = _make_state(spread_bps=100)
        sig = _make_signal()
        # current_ts = 1030000 ms (30 sec later, still in cooldown)
        allowed, reason = engine.check(sig, state, 0, 0.0, 0.0, 10000.0, current_ts=1030000)
        assert not allowed
        assert "cooldown" in reason

    def test_allows_after_cooldown(self):
        cfg = RiskConfig(cooldown_after_loss_sec=60)
        engine = RiskEngine(cfg)
        engine.record_loss(1000000)
        state = _make_state(spread_bps=100)
        sig = _make_signal()
        # current_ts = 1070000 ms (70 sec later, past cooldown)
        allowed, _ = engine.check(sig, state, 0, 0.0, 0.0, 10000.0, current_ts=1070000)
        assert allowed


class TestRiskEngineHold:
    def test_hold_always_allowed(self):
        cfg = RiskConfig()
        engine = RiskEngine(cfg)
        state = _make_state(spread_bps=99999)  # terrible spread
        sig = _make_signal(action="hold")
        allowed, _ = engine.check(sig, state, 100, 999999.0, -999999.0, 1.0)
        assert allowed


class TestRiskEngineReset:
    def test_reset_clears_loss_ts(self):
        cfg = RiskConfig(cooldown_after_loss_sec=60)
        engine = RiskEngine(cfg)
        engine.record_loss(1000000)
        engine.reset()
        state = _make_state(spread_bps=100)
        sig = _make_signal()
        allowed, _ = engine.check(sig, state, 0, 0.0, 0.0, 10000.0, current_ts=1000001)
        assert allowed

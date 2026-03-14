"""
Run summary reporting — generates structured JSON for each backtest/paper run.
"""

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from execution.portfolio import Portfolio
from analysis.metrics import compute_metrics


def generate_run_summary(
    strategy_name: str,
    portfolio: Portfolio,
    risk_config: dict = None,
    strategy_config: dict = None,
    mode: str = "backtest",
    market_count: int = 0,
    event_count: int = 0,
    elapsed_sec: float = 0.0,
) -> dict:
    """
    Generate a complete run summary as a dict (JSON-serializable).

    Args:
        strategy_name: name of the strategy
        portfolio: the portfolio after the run
        risk_config: risk config as dict (from RiskConfig.__dict__)
        strategy_config: strategy config as dict
        mode: "backtest" or "paper"
        market_count: number of markets processed
        event_count: number of events processed
        elapsed_sec: wall clock time

    Returns:
        Summary dict with run_id, config, metrics, and trade log.
    """
    metrics = compute_metrics(portfolio)

    trades = []
    for t in portfolio.trades:
        trades.append({
            "ts": t.ts,
            "asset": t.asset,
            "slug": t.slug,
            "side": t.side,
            "size": t.size,
            "fill_price": round(t.fill_price, 6),
            "slippage_bps": round(t.slippage_bps, 1),
            "fee": round(t.fee, 6),
            "rationale": t.rationale,
        })

    settlements = []
    for s in portfolio.settled:
        settlements.append({
            "condition_id": s.position.condition_id,
            "asset": s.position.asset,
            "slug": s.position.slug,
            "side": s.position.side,
            "size": s.position.size,
            "entry_price": round(s.position.entry_price, 6),
            "outcome": s.outcome,
            "payout": round(s.payout, 4),
            "pnl": round(s.pnl, 4),
            "settled_ts": s.settled_ts,
        })

    return {
        "run_id": str(uuid.uuid4())[:8],
        "timestamp": int(time.time()),
        "mode": mode,
        "strategy": strategy_name,
        "config": {
            "risk": risk_config or {},
            "strategy": strategy_config or {},
            "bankroll": portfolio.initial_bankroll,
        },
        "summary": {
            "market_count": market_count,
            "event_count": event_count,
            "elapsed_sec": round(elapsed_sec, 2),
        },
        "metrics": metrics,
        "portfolio": portfolio.summary(),
        "trades": trades,
        "settlements": settlements,
    }


def save_run_summary(summary: dict, output_dir: str = "runs") -> str:
    """
    Save a run summary to a JSON file.

    Args:
        summary: the run summary dict
        output_dir: directory to save to (created if doesn't exist)

    Returns:
        Path to the saved file.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    filename = f"{summary['run_id']}_{summary['strategy']}_{summary['mode']}.json"
    path = out / filename

    with open(path, "w") as f:
        json.dump(summary, f, indent=2)

    return str(path)


def print_trade_log(portfolio: Portfolio):
    """Print detailed per-trade log."""
    if not portfolio.trades:
        print("  No trades.")
        return

    print(f"\n  {'TS':<14} {'Side':<10} {'Asset':<6} {'Size':>6} {'Price':>8} "
          f"{'Slip':>6} {'Fee':>8} {'Rationale'}")
    print(f"  {'-'*14} {'-'*10} {'-'*6} {'-'*6} {'-'*8} {'-'*6} {'-'*8} {'-'*20}")

    for t in portfolio.trades:
        ts_str = str(t.ts)[-10:]  # last 10 digits for readability
        print(f"  {ts_str:<14} {t.side:<10} {t.asset:<6} {t.size:>6.0f} "
              f"${t.fill_price:>7.4f} {t.slippage_bps:>5.0f}bp ${t.fee:>7.4f} "
              f"{t.rationale[:20]}")

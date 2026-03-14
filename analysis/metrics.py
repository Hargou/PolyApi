"""
Metrics for backtest and paper trading runs.
"""

import math
from typing import List

from execution.portfolio import Portfolio, SettledPosition


def compute_metrics(portfolio: Portfolio) -> dict:
    """Compute all metrics from a completed run."""
    settled = portfolio.settled
    trades = portfolio.trades

    if not settled:
        return {
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "total_fees": 0.0,
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "max_win": 0.0,
            "max_loss": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "profit_factor": 0.0,
        }

    pnls = [s.pnl for s in settled]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross_pnl = sum(p + s.position.entry_fee for s, p in zip(settled, pnls))
    net_pnl = sum(pnls)
    total_wins = sum(wins) if wins else 0.0
    total_losses = abs(sum(losses)) if losses else 0.0

    # Max drawdown
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for p in pnls:
        running += p
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "net_pnl": round(net_pnl, 2),
        "gross_pnl": round(gross_pnl, 2),
        "total_fees": round(portfolio.total_fees, 4),
        "trade_count": len(settled),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(settled) * 100, 1) if settled else 0.0,
        "avg_pnl": round(net_pnl / len(settled), 2) if settled else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "max_win": round(max(wins), 2) if wins else 0.0,
        "max_loss": round(min(losses), 2) if losses else 0.0,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd / portfolio.initial_bankroll * 100, 2) if portfolio.initial_bankroll > 0 else 0.0,
        "profit_factor": round(total_wins / total_losses, 2) if total_losses > 0 else float('inf') if total_wins > 0 else 0.0,
    }


def print_summary(strategy_name: str, metrics: dict, portfolio: Portfolio):
    """Print a formatted summary of backtest results."""
    m = metrics
    print()
    print("=" * 60)
    print(f"  BACKTEST RESULTS: {strategy_name}")
    print("=" * 60)
    print()
    print(f"  Net PnL:          ${m['net_pnl']:>10.2f}")
    print(f"  Total Fees:       ${m['total_fees']:>10.4f}")
    print(f"  Bankroll:         ${portfolio.bankroll:>10.2f}  (started ${portfolio.initial_bankroll:.2f})")
    print()
    print(f"  Trades:           {m['trade_count']:>10}")
    print(f"  Wins:             {m['win_count']:>10}")
    print(f"  Losses:           {m['loss_count']:>10}")
    print(f"  Win Rate:         {m['win_rate']:>9.1f}%")
    print()
    print(f"  Avg PnL/trade:    ${m['avg_pnl']:>10.2f}")
    print(f"  Avg Win:          ${m['avg_win']:>10.2f}")
    print(f"  Avg Loss:         ${m['avg_loss']:>10.2f}")
    print(f"  Max Win:          ${m['max_win']:>10.2f}")
    print(f"  Max Loss:         ${m['max_loss']:>10.2f}")
    print()
    print(f"  Max Drawdown:     ${m['max_drawdown']:>10.2f}  ({m['max_drawdown_pct']:.2f}%)")
    print(f"  Profit Factor:    {m['profit_factor']:>10.2f}")
    print()
    print("=" * 60)

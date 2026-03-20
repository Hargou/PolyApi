"""Quick test: run Rust replay engine on the Parquet data."""
import sys
import time

# Already tested on current dataset — see docs/research/backtest_results.md
# from strategies.early_fade import EarlyFadeStrategy           # +$291, 41t, 53.7%, PF 1.22
# from strategies.early_fade_v2 import EarlyFadeV2Strategy      # +$291, 39t, 53.8%, PF 1.23
# from strategies.fee_extremes import FeeExtremesStrategy        # +$291, 41t, 53.7%, PF 1.22
# from strategies.combo_alpha import ComboAlphaStrategy          # +$460, 80t, 52.5%, PF 1.17
# from strategies.combo_alpha_v2 import ComboAlphaV2Strategy     # +$462, 78t, 53.8%, PF 1.17
# from strategies.binary_reversal import BinaryReversalStrategy  # +$134, 37t, 51.4%, PF 1.11
# from strategies.microstructure_fade import MicrostructureFadeStrategy  # 0 trades (needs L2)

# Active research
from strategies.combo_alpha_v2 import ComboAlphaV2Strategy
from strategies.combo_alpha_v4 import ComboAlphaV4Strategy

import poly_engine

PARQUET = "data_store/replay_data.parquet"

def main():
    strats = [
        # Baseline for comparison
        ComboAlphaV2Strategy(),        # +$462, 78t, 53.8%, PF 1.17

        # New: asset-specific extreme thresholds (investigation 006)
        ComboAlphaV4Strategy(),
    ]

    callbacks = [(s.name, s.evaluate) for s in strats]

    print(f"Running {len(callbacks)} strategies on {PARQUET}")
    print(f"Strategies: {[s.name for s in strats]}")
    print()

    t0 = time.perf_counter()
    results = poly_engine.run_replay(PARQUET, callbacks, 10_000.0)
    elapsed = time.perf_counter() - t0

    print(f"\n{'='*70}")
    print(f"Completed in {elapsed:.1f}s")
    print(f"{'='*70}\n")

    for r in results:
        print(f"  {r.strategy_name:25s} | PnL ${r.net_pnl:>8.2f} | "
              f"trades={r.trade_count:>4d} | win={r.win_rate:>5.1f}% | "
              f"PF={r.profit_factor:>5.2f} | DD=${r.max_drawdown:>7.2f} | "
              f"bank=${r.final_bankroll:>9.2f}")

    print()

if __name__ == "__main__":
    main()

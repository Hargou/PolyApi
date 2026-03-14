"""
Benchmark strategies for comparison.
Any real strategy must beat these to be worth trading.
"""

import random

from strategies.base import BaseStrategy, MarketState, Signal


class AlwaysYesStrategy(BaseStrategy):
    """Always buy YES at 50 contracts. Naive baseline."""
    name = "always_yes"

    def evaluate(self, state: MarketState) -> Signal:
        if state.elapsed_sec < 30 or state.elapsed_sec > 180:
            return Signal("hold", 0, 0, "timing filter")
        return Signal("buy_yes", 50, 300, "always yes")


class AlwaysNoStrategy(BaseStrategy):
    """Always buy NO at 50 contracts. Naive baseline."""
    name = "always_no"

    def evaluate(self, state: MarketState) -> Signal:
        if state.elapsed_sec < 30 or state.elapsed_sec > 180:
            return Signal("hold", 0, 0, "timing filter")
        return Signal("buy_no", 50, 300, "always no")


class RandomStrategy(BaseStrategy):
    """Random entry: 33% YES, 33% NO, 33% hold. Noise baseline."""
    name = "random"

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)

    def evaluate(self, state: MarketState) -> Signal:
        if state.elapsed_sec < 30 or state.elapsed_sec > 180:
            return Signal("hold", 0, 0, "timing filter")

        roll = self._rng.random()
        if roll < 0.33:
            return Signal("buy_yes", 50, 300, "random yes")
        elif roll < 0.66:
            return Signal("buy_no", 50, 300, "random no")
        else:
            return Signal("hold", 0, 0, "random hold")

    def reset(self):
        self._rng = random.Random(42)

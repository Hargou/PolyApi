"""
L2 order book fill simulator.
Walks bid/ask levels to compute realistic fill prices and slippage.
"""

from dataclasses import dataclass
from typing import List, Tuple

from execution.fees import taker_fee


@dataclass
class FillResult:
    """Result of a simulated fill."""
    filled: bool
    avg_price: float        # volume-weighted average fill price
    filled_size: float      # how many contracts were filled
    unfilled_size: float    # how many couldn't be filled (insufficient depth)
    slippage_bps: float     # slippage vs reference price in basis points
    fee: float              # taker fee on the fill
    total_cost: float       # filled_size * avg_price + fee


def walk_book(levels: List[Tuple[float, float]], target_size: float) -> Tuple[float, float, float]:
    """
    Walk order book levels to simulate a market order fill.

    Args:
        levels: [(price, size), ...] sorted best-to-worst
        target_size: number of contracts to fill

    Returns:
        (avg_fill_price, filled_size, unfilled_size)
    """
    remaining = float(target_size)
    filled = 0.0
    notional = 0.0

    for price, size in levels:
        p = float(price)
        s = float(size)
        take = min(remaining, s)
        notional += take * p
        filled += take
        remaining -= take
        if remaining <= 0:
            break

    avg_price = notional / filled if filled > 0 else 0.0
    return avg_price, filled, max(0.0, target_size - filled)


def simulate_fill(
    side: str,
    size: float,
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    max_slippage_bps: float = 500.0,
    fee_rate: float = 0.25,
    fee_exponent: int = 2,
) -> FillResult:
    """
    Simulate filling a market order against the order book.

    Args:
        side: "buy_yes" (lift asks) or "buy_no" (hit bids, since no_price = 1 - yes_price)
        size: number of contracts
        bids: [(price, size), ...] sorted highest first
        asks: [(price, size), ...] sorted lowest first
        max_slippage_bps: reject if slippage exceeds this
        fee_rate: Polymarket fee rate
        fee_exponent: Polymarket fee exponent

    Returns:
        FillResult with fill details.
    """
    if size <= 0:
        return FillResult(filled=False, avg_price=0.0, filled_size=0.0,
                          unfilled_size=0.0, slippage_bps=0.0, fee=0.0, total_cost=0.0)

    if side == "buy_yes":
        # Buying YES = lifting asks (buying at ask prices, lowest first)
        if not asks:
            return FillResult(filled=False, avg_price=0.0, filled_size=0.0,
                              unfilled_size=size, slippage_bps=0.0, fee=0.0, total_cost=0.0)
        levels = asks
        reference = float(asks[0][0])  # best ask = reference price
    elif side == "buy_no":
        # Buying NO = effectively selling YES = hitting bids (highest first)
        # NO price = 1 - YES price. We buy NO by selling YES tokens at bid.
        if not bids:
            return FillResult(filled=False, avg_price=0.0, filled_size=0.0,
                              unfilled_size=size, slippage_bps=0.0, fee=0.0, total_cost=0.0)
        levels = bids
        reference = float(bids[0][0])
    else:
        return FillResult(filled=False, avg_price=0.0, filled_size=0.0,
                          unfilled_size=size, slippage_bps=0.0, fee=0.0, total_cost=0.0)

    avg_price, filled_size, unfilled = walk_book(levels, size)

    if filled_size <= 0:
        return FillResult(filled=False, avg_price=0.0, filled_size=0.0,
                          unfilled_size=size, slippage_bps=0.0, fee=0.0, total_cost=0.0)

    # Slippage: how much worse than the best price
    if reference > 0:
        if side == "buy_yes":
            slippage_bps = (avg_price - reference) / reference * 10_000
        else:
            # For buy_no (hitting bids), slippage = how much lower than best bid
            slippage_bps = (reference - avg_price) / reference * 10_000
    else:
        slippage_bps = 0.0

    # Reject if slippage too high
    if slippage_bps > max_slippage_bps:
        return FillResult(filled=False, avg_price=avg_price, filled_size=0.0,
                          unfilled_size=size, slippage_bps=slippage_bps, fee=0.0, total_cost=0.0)

    fee = taker_fee(avg_price, filled_size, fee_rate, fee_exponent)
    total_cost = filled_size * avg_price + fee

    return FillResult(
        filled=True,
        avg_price=avg_price,
        filled_size=filled_size,
        unfilled_size=unfilled,
        slippage_bps=slippage_bps,
        fee=fee,
        total_cost=total_cost,
    )


def book_depth(levels: List[Tuple[float, float]]) -> float:
    """Total dollar depth across all levels."""
    return sum(float(p) * float(s) for p, s in levels)

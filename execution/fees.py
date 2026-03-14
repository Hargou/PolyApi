"""
Polymarket fee model.
Verified from https://docs.polymarket.com/trading/fees

Crypto markets: non-linear taker fee, makers pay zero (get 20% rebate).
Formula: fee = size * price * fee_rate * (price * (1 - price)) ^ exponent
Max effective rate ~1.56% at 50% probability.
"""


def taker_fee(price: float, size: float, fee_rate: float = 0.25, exponent: int = 2) -> float:
    """
    Compute Polymarket taker fee for crypto markets.

    Args:
        price: contract price (0.0 to 1.0, e.g. 0.52 for 52%)
        size: number of contracts
        fee_rate: base rate (0.25 for crypto)
        exponent: curve exponent (2 for crypto)

    Returns:
        Fee in dollars. Minimum 0.0001 (smaller amounts round to zero).
    """
    if price <= 0.0 or price >= 1.0 or size <= 0:
        return 0.0
    fee = size * price * fee_rate * (price * (1.0 - price)) ** exponent
    return fee if fee >= 0.0001 else 0.0


def maker_rebate(fee: float, rebate_pct: float = 0.20) -> float:
    """Maker rebate = percentage of the taker fee."""
    return fee * rebate_pct


def effective_rate(price: float, fee_rate: float = 0.25, exponent: int = 2) -> float:
    """
    Effective fee rate as a percentage of notional at a given price.
    Useful for understanding fee drag at different probability levels.
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return fee_rate * (price * (1.0 - price)) ** exponent * 100.0


def round_trip_cost(entry_price: float, exit_price: float, size: float,
                    fee_rate: float = 0.25, exponent: int = 2) -> float:
    """
    Total fee cost for a round-trip (entry + exit).
    For hold-to-expiry, exit_price is either 1.0 (win) or 0.0 (lose) — no exit fee.
    """
    entry_fee = taker_fee(entry_price, size, fee_rate, exponent)
    exit_fee = taker_fee(exit_price, size, fee_rate, exponent) if 0.0 < exit_price < 1.0 else 0.0
    return entry_fee + exit_fee

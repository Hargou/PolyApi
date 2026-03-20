use crate::fees;
use crate::types::FillResult;

/// Walk order book levels best-to-worst, compute VWAP fill price.
pub fn walk_book(levels: &[(f64, f64)], target_size: f64) -> (f64, f64, f64) {
    // Returns (avg_price, filled_size, unfilled_size)
    if levels.is_empty() || target_size <= 0.0 {
        return (0.0, 0.0, target_size);
    }

    let mut remaining = target_size;
    let mut notional = 0.0;
    let mut filled = 0.0;

    for &(price, size) in levels {
        if remaining <= 0.0 {
            break;
        }
        let take = remaining.min(size);
        notional += take * price;
        filled += take;
        remaining -= take;
    }

    if filled > 0.0 {
        (notional / filled, filled, remaining)
    } else {
        (0.0, 0.0, target_size)
    }
}

/// Simulate a fill: walk the book, compute slippage and fees.
pub fn simulate_fill(
    side: &str,
    size: f64,
    bids: &[(f64, f64)],
    asks: &[(f64, f64)],
    max_slippage_bps: f64,
) -> FillResult {
    // Buy YES = lift asks (buy at ask prices)
    // Buy NO = hit bids, but price is (1 - bid_price) for NO contracts
    let (levels, is_buy_yes) = match side {
        "buy_yes" => (asks, true),
        "buy_no" => (bids, false),
        _ => {
            return FillResult {
                filled: false,
                avg_price: 0.0,
                filled_size: 0.0,
                unfilled_size: size,
                slippage_bps: 0.0,
                fee: 0.0,
                total_cost: 0.0,
            };
        }
    };

    if levels.is_empty() {
        return FillResult {
            filled: false,
            avg_price: 0.0,
            filled_size: 0.0,
            unfilled_size: size,
            slippage_bps: 0.0,
            fee: 0.0,
            total_cost: 0.0,
        };
    }

    let (avg_price, filled_size, unfilled_size) = walk_book(levels, size);

    if filled_size <= 0.0 {
        return FillResult {
            filled: false,
            avg_price: 0.0,
            filled_size: 0.0,
            unfilled_size: size,
            slippage_bps: 0.0,
            fee: 0.0,
            total_cost: 0.0,
        };
    }

    // Reference price is the best level
    let ref_price = levels[0].0;
    let slippage_bps = if ref_price > 0.0 {
        ((avg_price - ref_price) / ref_price * 10_000.0).abs()
    } else {
        0.0
    };

    // Check slippage limit
    if max_slippage_bps > 0.0 && slippage_bps > max_slippage_bps {
        return FillResult {
            filled: false,
            avg_price: 0.0,
            filled_size: 0.0,
            unfilled_size: size,
            slippage_bps,
            fee: 0.0,
            total_cost: 0.0,
        };
    }

    let fee = fees::taker_fee(avg_price, filled_size);
    let total_cost = filled_size * avg_price + fee;

    FillResult {
        filled: true,
        avg_price,
        filled_size,
        unfilled_size,
        slippage_bps,
        fee,
        total_cost,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_walk_book_simple() {
        let levels = vec![(0.50, 100.0), (0.52, 200.0)];
        let (avg, filled, unfilled) = walk_book(&levels, 150.0);
        // 100 @ 0.50 + 50 @ 0.52 = 50 + 26 = 76 / 150 = 0.5067
        assert!((avg - 0.5067).abs() < 0.001);
        assert!((filled - 150.0).abs() < 1e-6);
        assert!((unfilled - 0.0).abs() < 1e-6);
    }

    #[test]
    fn test_walk_book_partial() {
        let levels = vec![(0.50, 50.0)];
        let (avg, filled, unfilled) = walk_book(&levels, 100.0);
        assert!((avg - 0.50).abs() < 1e-6);
        assert!((filled - 50.0).abs() < 1e-6);
        assert!((unfilled - 50.0).abs() < 1e-6);
    }

    #[test]
    fn test_simulate_fill() {
        let asks = vec![(0.50, 200.0)];
        let result = simulate_fill("buy_yes", 100.0, &[], &asks, 500.0);
        assert!(result.filled);
        assert!((result.avg_price - 0.50).abs() < 1e-6);
        assert!((result.filled_size - 100.0).abs() < 1e-6);
        assert!(result.fee > 0.0);
    }
}

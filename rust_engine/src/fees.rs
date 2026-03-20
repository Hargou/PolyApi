/// Polymarket non-linear taker fee.
/// fee = size * price * 0.25 * (price * (1 - price))^2
pub fn taker_fee(price: f64, size: f64) -> f64 {
    if price <= 0.0 || price >= 1.0 {
        return 0.0;
    }
    let p_q = price * (1.0 - price);
    size * price * 0.25 * p_q * p_q
}

/// Effective fee rate at a given price (fee per dollar notional).
pub fn effective_rate(price: f64) -> f64 {
    if price <= 0.0 || price >= 1.0 {
        return 0.0;
    }
    0.25 * (price * (1.0 - price)).powi(2)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fee_at_50c() {
        let fee = taker_fee(0.5, 100.0);
        // 100 * 0.5 * 0.25 * (0.5 * 0.5)^2 = 50 * 0.25 * 0.0625 = 0.78125
        assert!((fee - 0.78125).abs() < 1e-6);
    }

    #[test]
    fn test_fee_at_extremes() {
        let fee_low = taker_fee(0.05, 100.0);
        let fee_high = taker_fee(0.95, 100.0);
        assert!(fee_low < 0.01);
        assert!(fee_high < 0.01);
    }

    #[test]
    fn test_zero_price() {
        assert_eq!(taker_fee(0.0, 100.0), 0.0);
        assert_eq!(taker_fee(1.0, 100.0), 0.0);
    }
}

use std::collections::HashMap;
use crate::types::{MarketInfo, MarketState};

/// Build a MarketState from current data. Returns None if insufficient data.
pub fn build_market_state(
    market: &MarketInfo,
    ts: i64,
    best_bid: f64,
    best_ask: f64,
    bids: &[(f64, f64)],
    asks: &[(f64, f64)],
    last_trade_price: Option<f64>,
    spot_prices: &HashMap<String, f64>,
    spot_at_window_start: &HashMap<String, f64>,
) -> Option<MarketState> {
    let sym = format!("{}usdt", market.asset.to_lowercase());
    let spot = *spot_prices.get(&sym)?;
    let spot_start = *spot_at_window_start
        .get(&market.condition_id)
        .unwrap_or(&spot);

    let now_sec = ts / 1000;
    let elapsed = (now_sec - market.window_start_ts).max(0);
    let remaining = (market.window_end_ts - now_sec).max(0);

    let midpoint = if (best_bid + best_ask) > 0.0 {
        (best_bid + best_ask) / 2.0
    } else {
        0.5
    };
    let spread = best_ask - best_bid;
    let spread_bps = if midpoint > 0.0 {
        spread / midpoint * 10_000.0
    } else {
        0.0
    };

    let bid_depth: f64 = bids.iter().map(|(p, s)| p * s).sum();
    let ask_depth: f64 = asks.iter().map(|(p, s)| p * s).sum();

    let spot_return = if spot_start > 0.0 {
        (spot - spot_start) / spot_start * 10_000.0
    } else {
        0.0
    };

    // Microstructure
    let bid_size_best = bids.first().map(|(_, s)| *s).unwrap_or(0.0);
    let ask_size_best = asks.first().map(|(_, s)| *s).unwrap_or(0.0);

    let microprice = if bid_size_best + ask_size_best > 0.0 {
        (best_bid * ask_size_best + best_ask * bid_size_best) / (bid_size_best + ask_size_best)
    } else {
        midpoint
    };

    let total_depth = bid_depth + ask_depth;
    let obi = if total_depth > 0.0 {
        (bid_depth - ask_depth) / total_depth
    } else {
        0.0
    };

    // Cross-asset spot returns
    let mut other_returns = HashMap::new();
    for (other_sym, &other_spot) in spot_prices {
        let other_asset = other_sym.replace("usdt", "").to_uppercase();
        if other_asset != market.asset {
            let key = format!("_global_{}", other_sym);
            let other_start = *spot_at_window_start.get(&key).unwrap_or(&other_spot);
            if other_start > 0.0 {
                other_returns.insert(
                    other_asset,
                    (other_spot - other_start) / other_start * 10_000.0,
                );
            }
        }
    }

    // Effective fee rate at midpoint
    let eff_fee = if midpoint > 0.0 && midpoint < 1.0 {
        0.25 * (midpoint * (1.0 - midpoint)).powi(2)
    } else {
        0.0
    };

    Some(MarketState {
        condition_id: market.condition_id.clone(),
        yes_token_id: market.yes_token_id.clone(),
        asset: market.asset.clone(),
        slug: market.slug.clone(),
        best_bid,
        best_ask,
        spread,
        spread_bps,
        midpoint,
        bid_depth,
        ask_depth,
        spot_price: spot,
        spot_price_at_window_start: spot_start,
        spot_return_bps: spot_return,
        window_start_ts: market.window_start_ts,
        window_end_ts: market.window_end_ts,
        elapsed_sec: elapsed,
        remaining_sec: remaining,
        ts,
        bids: bids.to_vec(),
        asks: asks.to_vec(),
        bid_size_at_best: bid_size_best,
        ask_size_at_best: ask_size_best,
        microprice,
        obi,
        other_spot_returns: other_returns,
        effective_fee_rate: eff_fee,
    })
}

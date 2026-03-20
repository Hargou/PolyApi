use pyo3::prelude::*;
use std::collections::HashMap;

/// MarketState passed to Python strategy.evaluate()
#[pyclass(get_all)]
#[derive(Clone, Debug)]
pub struct MarketState {
    // Identity
    pub condition_id: String,
    pub yes_token_id: String,
    pub asset: String,
    pub slug: String,

    // Order book
    pub best_bid: f64,
    pub best_ask: f64,
    pub spread: f64,
    pub spread_bps: f64,
    pub midpoint: f64,
    pub bid_depth: f64,
    pub ask_depth: f64,

    // Spot price
    pub spot_price: f64,
    pub spot_price_at_window_start: f64,
    pub spot_return_bps: f64,

    // Timing
    pub window_start_ts: i64,
    pub window_end_ts: i64,
    pub elapsed_sec: i64,
    pub remaining_sec: i64,
    pub ts: i64,

    // Microstructure
    pub bids: Vec<(f64, f64)>,
    pub asks: Vec<(f64, f64)>,
    pub bid_size_at_best: f64,
    pub ask_size_at_best: f64,
    pub microprice: f64,
    pub obi: f64,

    // Cross-asset
    pub other_spot_returns: HashMap<String, f64>,

    // Fees
    pub effective_fee_rate: f64,
}

#[pymethods]
impl MarketState {
    fn __repr__(&self) -> String {
        format!(
            "MarketState(asset={}, mid={:.3}, spread_bps={:.0}, spot_ret={:.1}bps, rem={}s)",
            self.asset, self.midpoint, self.spread_bps, self.spot_return_bps, self.remaining_sec
        )
    }
}

/// Signal returned by Python strategy.evaluate()
#[derive(Clone, Debug)]
pub struct Signal {
    pub action: String,      // "buy_yes", "buy_no", "hold"
    pub size: i64,
    pub max_slippage_bps: i64,
    pub rationale: String,
    pub p_hat: Option<f64>,
    pub ev_bps: Option<f64>,
}

impl Signal {
    pub fn from_pyobject(py: Python<'_>, obj: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Signal {
            action: obj.getattr("action")?.extract::<String>()?,
            size: obj.getattr("size")?.extract::<i64>()?,
            max_slippage_bps: obj.getattr("max_slippage_bps")?.extract::<i64>()?,
            rationale: obj.getattr("rationale")?.extract::<String>().unwrap_or_default(),
            p_hat: obj.getattr("p_hat")?.extract::<Option<f64>>().ok().flatten(),
            ev_bps: obj.getattr("ev_bps")?.extract::<Option<f64>>().ok().flatten(),
        })
    }
}

/// Internal market info (not exposed to Python)
#[derive(Clone, Debug)]
pub struct MarketInfo {
    pub condition_id: String,
    pub yes_token_id: String,
    pub asset: String,
    pub slug: String,
    pub window_start_ts: i64,
    pub window_end_ts: i64,
    pub question: String,
    pub volume: f64,
    pub liquidity: f64,
}

/// Fill result from book walk
#[pyclass(get_all)]
#[derive(Clone, Debug)]
pub struct FillResult {
    pub filled: bool,
    pub avg_price: f64,
    pub filled_size: f64,
    pub unfilled_size: f64,
    pub slippage_bps: f64,
    pub fee: f64,
    pub total_cost: f64,
}

/// Position tracked in portfolio
#[derive(Clone, Debug)]
pub struct Position {
    pub condition_id: String,
    pub yes_token_id: String,
    pub asset: String,
    pub slug: String,
    pub side: String,       // "yes" or "no"
    pub size: f64,
    pub entry_price: f64,
    pub entry_ts: i64,
    pub entry_fee: f64,
    pub window_end_ts: i64,
}

/// Trade record
#[derive(Clone, Debug)]
pub struct Trade {
    pub ts: i64,
    pub condition_id: String,
    pub asset: String,
    pub slug: String,
    pub side: String,
    pub size: f64,
    pub fill_price: f64,
    pub slippage_bps: f64,
    pub fee: f64,
    pub rationale: String,
}

/// Settled position
#[derive(Clone, Debug)]
pub struct SettledPosition {
    pub position: Position,
    pub outcome: String,
    pub payout: f64,
    pub pnl: f64,
    pub settled_ts: i64,
}

/// Risk configuration
#[derive(Clone, Debug)]
pub struct RiskConfig {
    pub max_position_per_market: i64,
    pub max_total_exposure: f64,
    pub max_concurrent_positions: i64,
    pub max_loss_per_window: f64,
    pub max_drawdown_pct: f64,
    pub max_spread_bps: f64,
    pub min_remaining_sec: i64,
    pub max_elapsed_sec: i64,
    pub cooldown_after_loss_sec: i64,
}

impl Default for RiskConfig {
    fn default() -> Self {
        RiskConfig {
            max_position_per_market: 500,
            max_total_exposure: 5000.0,
            max_concurrent_positions: 6,
            max_loss_per_window: 200.0,
            max_drawdown_pct: 10.0,
            max_spread_bps: 1000.0,
            min_remaining_sec: 5,
            max_elapsed_sec: 295,
            cooldown_after_loss_sec: 0,
        }
    }
}

/// Per-strategy backtest results
#[pyclass(get_all)]
#[derive(Clone, Debug)]
pub struct BacktestResult {
    pub strategy_name: String,
    pub net_pnl: f64,
    pub total_fees: f64,
    pub trade_count: i64,
    pub win_count: i64,
    pub loss_count: i64,
    pub win_rate: f64,
    pub max_drawdown: f64,
    pub profit_factor: f64,
    pub avg_pnl_per_trade: f64,
    pub final_bankroll: f64,
}

#[pymethods]
impl BacktestResult {
    fn __repr__(&self) -> String {
        format!(
            "BacktestResult({}: pnl=${:.2}, trades={}, win={:.1}%, pf={:.2})",
            self.strategy_name, self.net_pnl, self.trade_count, self.win_rate, self.profit_factor
        )
    }
}

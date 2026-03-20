use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::time::Instant;

use arrow::array::*;
use arrow::datatypes::DataType;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use pyo3::prelude::*;

use crate::fees;
use crate::fill;
use crate::state;
use crate::types::*;

/// Parse bids/asks JSON string into Vec<(f64, f64)>
fn parse_levels(json_str: &str) -> Vec<(f64, f64)> {
    if json_str.is_empty() || json_str == "[]" {
        return Vec::new();
    }
    // Parse as array of objects or array of arrays
    let parsed: Result<Vec<serde_json::Value>, _> = serde_json::from_str(json_str);
    match parsed {
        Ok(items) => {
            let mut levels = Vec::with_capacity(items.len());
            for item in &items {
                if let Some(obj) = item.as_object() {
                    let p = obj
                        .get("price")
                        .and_then(|v| v.as_str().map(|s| s.parse::<f64>().unwrap_or(0.0))
                            .or_else(|| v.as_f64()))
                        .unwrap_or(0.0);
                    let s = obj
                        .get("size")
                        .and_then(|v| v.as_str().map(|s| s.parse::<f64>().unwrap_or(0.0))
                            .or_else(|| v.as_f64()))
                        .unwrap_or(0.0);
                    if p > 0.0 && s > 0.0 {
                        levels.push((p, s));
                    }
                } else if let Some(arr) = item.as_array() {
                    if arr.len() >= 2 {
                        let p = arr[0].as_f64().unwrap_or(0.0);
                        let s = arr[1].as_f64().unwrap_or(0.0);
                        if p > 0.0 && s > 0.0 {
                            levels.push((p, s));
                        }
                    }
                }
            }
            levels
        }
        Err(_) => Vec::new(),
    }
}

/// Helper to get string from a column at a row index
fn get_str<'a>(col: &'a StringArray, idx: usize) -> &'a str {
    if col.is_null(idx) {
        ""
    } else {
        col.value(idx)
    }
}

/// Helper to get f64 from a column, returning None if null
fn get_f64(col: &Float64Array, idx: usize) -> Option<f64> {
    if col.is_null(idx) {
        None
    } else {
        Some(col.value(idx))
    }
}

/// Helper to get i64 from a column
fn get_i64(col: &Int64Array, idx: usize) -> i64 {
    if col.is_null(idx) {
        0
    } else {
        col.value(idx)
    }
}

/// Per-strategy runner state
struct StrategyRunner {
    name: String,
    callback: PyObject,        // Python strategy.evaluate
    spot_prices: HashMap<String, f64>,
    spot_at_window_start: HashMap<String, f64>,
    markets: HashMap<String, MarketInfo>,
    processed_markets: HashSet<String>,

    // Portfolio state
    bankroll: f64,
    initial_bankroll: f64,
    positions: HashMap<String, Position>,
    trades: Vec<Trade>,
    settled: Vec<SettledPosition>,
    total_fees: f64,
    peak_bankroll: f64,
    max_drawdown: f64,

    // Risk state
    risk_config: RiskConfig,
    last_loss_ts: i64,
}

impl StrategyRunner {
    fn new(name: String, callback: PyObject, bankroll: f64) -> Self {
        let initial = bankroll;
        StrategyRunner {
            name,
            callback,
            spot_prices: HashMap::new(),
            spot_at_window_start: HashMap::new(),
            markets: HashMap::new(),
            processed_markets: HashSet::new(),
            bankroll,
            initial_bankroll: initial,
            positions: HashMap::new(),
            trades: Vec::new(),
            settled: Vec::new(),
            total_fees: 0.0,
            peak_bankroll: bankroll,
            max_drawdown: 0.0,
            risk_config: RiskConfig::default(),
            last_loss_ts: 0,
        }
    }

    fn handle_spot(&mut self, sym: &str, price: f64) {
        self.spot_prices.insert(sym.to_string(), price);
    }

    fn handle_market_info(&mut self, info: MarketInfo) {
        let sym = format!("{}usdt", info.asset.to_lowercase());
        if let Some(&spot) = self.spot_prices.get(&sym) {
            self.spot_at_window_start
                .insert(info.condition_id.clone(), spot);
        }
        // Snapshot all spot prices for cross-asset
        for (s, &p) in &self.spot_prices {
            self.spot_at_window_start
                .insert(format!("_global_{}", s), p);
        }
        self.markets.insert(info.condition_id.clone(), info);
    }

    fn handle_resolution(&mut self, condition_id: &str, outcome: &str, ts: i64) {
        if let Some(pos) = self.positions.remove(condition_id) {
            let won = (pos.side == "yes" && outcome == "yes")
                || (pos.side == "no" && outcome == "no");
            let payout = if won { pos.size * 1.0 } else { 0.0 };
            let cost = pos.size * pos.entry_price + pos.entry_fee;
            let pnl = payout - cost;

            self.bankroll += payout;
            if pnl < 0.0 {
                self.last_loss_ts = ts;
            }

            // Track drawdown
            if self.bankroll > self.peak_bankroll {
                self.peak_bankroll = self.bankroll;
            }
            let dd = self.peak_bankroll - self.bankroll;
            if dd > self.max_drawdown {
                self.max_drawdown = dd;
            }

            self.settled.push(SettledPosition {
                position: pos,
                outcome: outcome.to_string(),
                payout,
                pnl,
                settled_ts: ts,
            });
        }

        // Cleanup
        self.processed_markets.remove(condition_id);
        self.markets.remove(condition_id);
        self.spot_at_window_start.remove(condition_id);
    }

    /// Check risk limits. Returns true if trade is allowed.
    fn risk_check(&self, state: &MarketState, size: f64, ts: i64) -> bool {
        let rc = &self.risk_config;

        // Position size limit
        if size > rc.max_position_per_market as f64 {
            return false;
        }

        // Concurrent positions
        if self.positions.len() >= rc.max_concurrent_positions as usize {
            return false;
        }

        // Spread check
        if state.spread_bps > rc.max_spread_bps {
            return false;
        }

        // Timing checks
        if state.remaining_sec < rc.min_remaining_sec {
            return false;
        }
        if state.elapsed_sec > rc.max_elapsed_sec {
            return false;
        }

        // Drawdown circuit breaker
        let dd_pct = if self.initial_bankroll > 0.0 {
            (self.peak_bankroll - self.bankroll) / self.initial_bankroll * 100.0
        } else {
            0.0
        };
        if dd_pct > rc.max_drawdown_pct {
            return false;
        }

        // Exposure check
        let current_exposure: f64 = self
            .positions
            .values()
            .map(|p| p.size * p.entry_price)
            .sum();
        if current_exposure + size * state.midpoint > rc.max_total_exposure {
            return false;
        }

        // Cooldown after loss
        if rc.cooldown_after_loss_sec > 0 && self.last_loss_ts > 0 {
            let since_loss = (ts / 1000) - (self.last_loss_ts / 1000);
            if since_loss < rc.cooldown_after_loss_sec {
                return false;
            }
        }

        true
    }

    fn open_position(
        &mut self,
        state: &MarketState,
        side: &str,
        fill_result: &FillResult,
        signal: &Signal,
        ts: i64,
    ) {
        let cost = fill_result.filled_size * fill_result.avg_price + fill_result.fee;
        if cost > self.bankroll {
            return;
        }

        self.bankroll -= cost;
        self.total_fees += fill_result.fee;

        let pos = Position {
            condition_id: state.condition_id.clone(),
            yes_token_id: state.yes_token_id.clone(),
            asset: state.asset.clone(),
            slug: state.slug.clone(),
            side: if side == "buy_yes" {
                "yes".to_string()
            } else {
                "no".to_string()
            },
            size: fill_result.filled_size,
            entry_price: fill_result.avg_price,
            entry_ts: ts,
            entry_fee: fill_result.fee,
            window_end_ts: state.window_end_ts,
        };

        let trade = Trade {
            ts,
            condition_id: state.condition_id.clone(),
            asset: state.asset.clone(),
            slug: state.slug.clone(),
            side: side.to_string(),
            size: fill_result.filled_size,
            fill_price: fill_result.avg_price,
            slippage_bps: fill_result.slippage_bps,
            fee: fill_result.fee,
            rationale: signal.rationale.clone(),
        };

        self.positions.insert(state.condition_id.clone(), pos);
        self.trades.push(trade);
        self.processed_markets.insert(state.condition_id.clone());
    }

    fn compute_result(&self) -> BacktestResult {
        let wins: Vec<&SettledPosition> = self.settled.iter().filter(|s| s.pnl > 0.0).collect();
        let losses: Vec<&SettledPosition> = self.settled.iter().filter(|s| s.pnl <= 0.0).collect();
        let total_wins: f64 = wins.iter().map(|s| s.pnl).sum();
        let total_losses: f64 = losses.iter().map(|s| s.pnl.abs()).sum();
        let trade_count = self.trades.len() as i64;
        let win_rate = if !self.settled.is_empty() {
            wins.len() as f64 / self.settled.len() as f64 * 100.0
        } else {
            0.0
        };
        let profit_factor = if total_losses > 0.0 {
            total_wins / total_losses
        } else if total_wins > 0.0 {
            f64::INFINITY
        } else {
            0.0
        };
        let net_pnl = self.bankroll - self.initial_bankroll;
        let avg_pnl = if !self.settled.is_empty() {
            net_pnl / self.settled.len() as f64
        } else {
            0.0
        };

        BacktestResult {
            strategy_name: self.name.clone(),
            net_pnl,
            total_fees: self.total_fees,
            trade_count,
            win_count: wins.len() as i64,
            loss_count: losses.len() as i64,
            win_rate,
            max_drawdown: self.max_drawdown,
            profit_factor,
            avg_pnl_per_trade: avg_pnl,
            final_bankroll: self.bankroll,
        }
    }
}

/// Main replay function callable from Python.
/// Reads Parquet, runs strategies, returns results.
#[pyfunction]
pub fn run_replay(
    py: Python<'_>,
    parquet_path: &str,
    strategy_callbacks: Vec<(String, PyObject)>,
    bankroll: f64,
) -> PyResult<Vec<BacktestResult>> {
    let t_start = Instant::now();

    // --- Read Parquet ---
    let file = std::fs::File::open(Path::new(parquet_path))
        .map_err(|e| pyo3::exceptions::PyIOError::new_err(format!("Cannot open {}: {}", parquet_path, e)))?;

    let builder = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Parquet error: {}", e)))?;

    let reader = builder.build()
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Parquet reader error: {}", e)))?;

    eprintln!("[rust] Parquet opened in {:.1}s", t_start.elapsed().as_secs_f64());

    // Build runners
    let mut runners: Vec<StrategyRunner> = strategy_callbacks
        .into_iter()
        .map(|(name, cb)| StrategyRunner::new(name, cb, bankroll))
        .collect();

    let mut token_to_cid: HashMap<String, String> = HashMap::new();

    let mut n_total: usize = 0;
    let mut n_spot: usize = 0;
    let mut n_clob: usize = 0;
    let mut n_clob_eval: usize = 0;
    let mut n_market: usize = 0;
    let mut n_res: usize = 0;

    let t_loop = Instant::now();

    // --- Process record batches ---
    for batch_result in reader {
        let batch = batch_result
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("Batch error: {}", e)))?;

        let num_rows = batch.num_rows();

        // Extract columns by name
        let type_col = batch.column_by_name("type")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>())
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("Missing 'type' column"))?;
        let ts_col = batch.column_by_name("ts")
            .and_then(|c| c.as_any().downcast_ref::<Int64Array>())
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("Missing 'ts' column"))?;

        // Optional columns (may not exist in all rows but columns exist)
        let sym_col = batch.column_by_name("sym")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let price_col = batch.column_by_name("price")
            .and_then(|c| c.as_any().downcast_ref::<Float64Array>());
        let aid_col = batch.column_by_name("asset_id")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let cid_col = batch.column_by_name("condition_id")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let et_col = batch.column_by_name("event_type")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let bb_col = batch.column_by_name("best_bid")
            .and_then(|c| c.as_any().downcast_ref::<Float64Array>());
        let ba_col = batch.column_by_name("best_ask")
            .and_then(|c| c.as_any().downcast_ref::<Float64Array>());
        let ltp_col = batch.column_by_name("last_trade_price")
            .and_then(|c| c.as_any().downcast_ref::<Float64Array>());
        let bids_json_col = batch.column_by_name("bids_json")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let asks_json_col = batch.column_by_name("asks_json")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());

        // Market info columns
        let mi_cid_col = batch.column_by_name("mi_condition_id")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let mi_tid_col = batch.column_by_name("mi_yes_token_id")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let mi_asset_col = batch.column_by_name("mi_asset")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let mi_slug_col = batch.column_by_name("mi_slug")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let mi_wstart_col = batch.column_by_name("mi_window_start_ts")
            .and_then(|c| c.as_any().downcast_ref::<Int64Array>());
        let mi_wend_col = batch.column_by_name("mi_window_end_ts")
            .and_then(|c| c.as_any().downcast_ref::<Int64Array>());
        let mi_question_col = batch.column_by_name("mi_question")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let mi_volume_col = batch.column_by_name("mi_volume")
            .and_then(|c| c.as_any().downcast_ref::<Float64Array>());
        let mi_liquidity_col = batch.column_by_name("mi_liquidity")
            .and_then(|c| c.as_any().downcast_ref::<Float64Array>());

        // Resolution columns
        let res_cid_col = batch.column_by_name("res_condition_id")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());
        let res_outcome_col = batch.column_by_name("res_outcome")
            .and_then(|c| c.as_any().downcast_ref::<StringArray>());

        for i in 0..num_rows {
            let etype = get_str(type_col, i);
            let ts = get_i64(ts_col, i);

            match etype {
                "spot" | "chainlink" => {
                    if let (Some(sc), Some(pc)) = (sym_col, price_col) {
                        let sym = get_str(sc, i);
                        if let Some(price) = get_f64(pc, i) {
                            if !sym.is_empty() {
                                for runner in &mut runners {
                                    runner.handle_spot(sym, price);
                                }
                                n_spot += 1;
                            }
                        }
                    }
                }

                "market_info" => {
                    let cid = mi_cid_col.map(|c| get_str(c, i)).unwrap_or("");
                    let tid = mi_tid_col.map(|c| get_str(c, i)).unwrap_or("");
                    if !cid.is_empty() && !tid.is_empty() {
                        token_to_cid.insert(tid.to_string(), cid.to_string());
                        let info = MarketInfo {
                            condition_id: cid.to_string(),
                            yes_token_id: tid.to_string(),
                            asset: mi_asset_col.map(|c| get_str(c, i)).unwrap_or("").to_string(),
                            slug: mi_slug_col.map(|c| get_str(c, i)).unwrap_or("").to_string(),
                            window_start_ts: mi_wstart_col.map(|c| get_i64(c, i)).unwrap_or(0),
                            window_end_ts: mi_wend_col.map(|c| get_i64(c, i)).unwrap_or(0),
                            question: mi_question_col.map(|c| get_str(c, i)).unwrap_or("").to_string(),
                            volume: mi_volume_col.and_then(|c| get_f64(c, i)).unwrap_or(0.0),
                            liquidity: mi_liquidity_col.and_then(|c| get_f64(c, i)).unwrap_or(0.0),
                        };
                        for runner in &mut runners {
                            runner.handle_market_info(info.clone());
                        }
                        n_market += 1;
                    }
                }

                "clob" => {
                    n_clob += 1;
                    let aid = aid_col.map(|c| get_str(c, i)).unwrap_or("");
                    let cid_val = cid_col.map(|c| get_str(c, i)).unwrap_or("");
                    let cid = if !cid_val.is_empty() {
                        cid_val.to_string()
                    } else {
                        token_to_cid.get(aid).cloned().unwrap_or_default()
                    };

                    if cid.is_empty() {
                        continue;
                    }

                    // Check if ANY runner has this market and hasn't fully processed it
                    let any_needs = runners.iter().any(|r| {
                        r.markets.contains_key(&cid) && !r.processed_markets.contains(&cid)
                    });
                    if !any_needs {
                        continue;
                    }

                    // Build book data
                    let evt = et_col.map(|c| get_str(c, i)).unwrap_or("");
                    let bb = bb_col.and_then(|c| get_f64(c, i));
                    let ba = ba_col.and_then(|c| get_f64(c, i));
                    let ltp = ltp_col.and_then(|c| get_f64(c, i));

                    let (bids, asks, best_bid, best_ask) = if evt == "book" {
                        let bids_str = bids_json_col.map(|c| get_str(c, i)).unwrap_or("[]");
                        let asks_str = asks_json_col.map(|c| get_str(c, i)).unwrap_or("[]");
                        let bids = parse_levels(bids_str);
                        let asks = parse_levels(asks_str);
                        let bb_val = bb.unwrap_or_else(|| bids.first().map(|(p, _)| *p).unwrap_or(0.0));
                        let ba_val = ba.unwrap_or_else(|| asks.first().map(|(p, _)| *p).unwrap_or(1.0));
                        (bids, asks, bb_val, ba_val)
                    } else if let (Some(bb_v), Some(ba_v)) = (bb, ba) {
                        let bids = if bb_v > 0.0 { vec![(bb_v, 100.0)] } else { vec![] };
                        let asks = if ba_v < 1.0 { vec![(ba_v, 100.0)] } else { vec![] };
                        (bids, asks, bb_v, ba_v)
                    } else if let Some(ltp_v) = ltp {
                        (vec![], vec![], 0.0, 1.0)
                    } else {
                        continue;
                    };

                    n_clob_eval += 1;

                    // Evaluate each strategy
                    for runner in &mut runners {
                        if !runner.markets.contains_key(&cid) {
                            continue;
                        }
                        if runner.processed_markets.contains(&cid) {
                            continue;
                        }

                        // Build MarketState (pure Rust, no GIL)
                        let market = runner.markets.get(&cid).unwrap().clone();
                        let ms = state::build_market_state(
                            &market,
                            ts,
                            best_bid,
                            best_ask,
                            &bids,
                            &asks,
                            ltp,
                            &runner.spot_prices,
                            &runner.spot_at_window_start,
                        );

                        let ms = match ms {
                            Some(ms) => ms,
                            None => continue,
                        };

                        // Risk check (pure Rust)
                        if !runner.risk_check(&ms, 100.0, ts) {
                            continue;
                        }

                        // --- GIL: call Python strategy.evaluate() ---
                        let signal = {
                            let state_py = Py::new(py, ms.clone())?;
                            let result = runner.callback.call1(py, (state_py,))?;
                            Signal::from_pyobject(py, result.bind(py))?
                        };

                        if signal.action == "hold" {
                            continue;
                        }

                        // Fill simulation (pure Rust)
                        let fill_result = fill::simulate_fill(
                            &signal.action,
                            signal.size as f64,
                            &bids,
                            &asks,
                            signal.max_slippage_bps as f64,
                        );

                        if fill_result.filled {
                            runner.open_position(&ms, &signal.action, &fill_result, &signal, ts);
                        }
                    }
                }

                "resolution" => {
                    let cid = res_cid_col.map(|c| get_str(c, i)).unwrap_or("");
                    let outcome = res_outcome_col.map(|c| get_str(c, i)).unwrap_or("no");
                    if !cid.is_empty() {
                        for runner in &mut runners {
                            runner.handle_resolution(cid, outcome, ts);
                        }
                        n_res += 1;
                    }
                }

                _ => {}
            }

            n_total += 1;

            if n_total > 0 && n_total % 2_000_000 == 0 {
                eprintln!(
                    "[rust] {:.1}M rows ({:.1}s) | spot={} clob={}(eval={}) market={} res={}",
                    n_total as f64 / 1e6,
                    t_loop.elapsed().as_secs_f64(),
                    n_spot, n_clob, n_clob_eval, n_market, n_res
                );
            }
        }
    }

    let elapsed_loop = t_loop.elapsed().as_secs_f64();
    let elapsed_total = t_start.elapsed().as_secs_f64();

    eprintln!(
        "[rust] Complete: {:.1}M rows in {:.1}s (loop={:.1}s)",
        n_total as f64 / 1e6, elapsed_total, elapsed_loop
    );
    eprintln!(
        "[rust] spot={} clob={}(eval={}, skip={}) market={} res={}",
        n_spot, n_clob, n_clob_eval, n_clob - n_clob_eval, n_market, n_res
    );

    // Compute results
    let results: Vec<BacktestResult> = runners.iter().map(|r| r.compute_result()).collect();
    Ok(results)
}

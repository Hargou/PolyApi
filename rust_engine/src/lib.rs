mod types;
mod fees;
mod fill;
mod state;
mod replay;

use pyo3::prelude::*;

#[pymodule]
fn poly_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(replay::run_replay, m)?)?;
    m.add_class::<types::MarketState>()?;
    m.add_class::<types::FillResult>()?;
    m.add_class::<types::BacktestResult>()?;
    Ok(())
}

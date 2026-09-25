//! FT004 — a `OnceLock` initialiser with an observable side effect.
//!
//! `get_or_init` guarantees the value is produced once, but it does NOT
//! guarantee the closure runs once: racing threads may both run it, and only
//! one result is kept. Anything the closure does besides producing the value
//! can therefore happen twice.

use pyo3::prelude::*;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;

static INIT_COUNT: AtomicU64 = AtomicU64::new(0);
static CONFIG: OnceLock<String> = OnceLock::new();

#[pyfunction]
fn config() -> &'static str {
    CONFIG.get_or_init(|| {
        // Observable side effect: this can run more than once.
        INIT_COUNT.fetch_add(1, Ordering::SeqCst);
        "default".to_string()
    })
}

#[pyfunction]
fn init_count() -> u64 {
    INIT_COUNT.load(Ordering::SeqCst)
}

#[pymodule]
fn ft004_oncelock_side_effect(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(config, m)?)?;
    m.add_function(wrap_pyfunction!(init_count, m)?)
}

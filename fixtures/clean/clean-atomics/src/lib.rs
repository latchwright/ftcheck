//! Correct: a `Sync` global counter and a `OnceLock` whose initialiser is pure.

use pyo3::prelude::*;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;

static COUNTER: AtomicU64 = AtomicU64::new(0);
static NAME: OnceLock<String> = OnceLock::new();

#[pyfunction]
fn bump() -> u64 {
    COUNTER.fetch_add(1, Ordering::SeqCst)
}

#[pyfunction]
fn name() -> &'static str {
    NAME.get_or_init(|| "ftcheck".to_string())
}

#[pymodule]
fn clean_atomics(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bump, m)?)?;
    m.add_function(wrap_pyfunction!(name, m)?)
}

//! FT001 — unsynchronised `static mut` reachable from Python.
//!
//! Under the GIL this was merely ugly: two threads could not be inside `bump`
//! at once. Without the GIL it is a textbook data race — an unsynchronised
//! read-modify-write on shared memory.

use pyo3::prelude::*;

static mut COUNTER: u64 = 0;

#[pyfunction]
fn bump() -> u64 {
    unsafe {
        COUNTER += 1;
        COUNTER
    }
}

#[pymodule]
fn ft001_static_mut(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bump, m)?)
}

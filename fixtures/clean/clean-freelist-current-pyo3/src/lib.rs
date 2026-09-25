//! A `#[pyclass(freelist = N)]` type.
//!
//! This source is IDENTICAL in `racy/ft002-freelist-old-pyo3` and
//! `clean/clean-freelist-current-pyo3`. The two fixtures differ only in the
//! PyO3 version they pin. That is the whole point: whether this code is a
//! finding depends on the framework version, not on the code.

use pyo3::prelude::*;

#[pyclass(freelist = 8)]
struct Token {
    #[pyo3(get)]
    id: u64,
}

#[pymethods]
impl Token {
    #[new]
    fn new(id: u64) -> Self {
        Token { id }
    }
}


#[pymodule]
fn clean_freelist_current_pyo3(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Token>()
}

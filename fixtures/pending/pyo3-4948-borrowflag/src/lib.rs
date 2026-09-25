//! A reconstruction of the race PyO3 actually fixed from issue #4904.
//!
//! NOT a "pyclass type-object init" race — that trace in #4904 was a CPython
//! bug, reported upstream as python/cpython#130421. The race PyO3 fixed was in
//! `BorrowFlag`, the runtime borrow checker for mutable pyclass instances,
//! via PR #4948, first released in 0.24.0.
//!
//! This crate pins 0.23.5 — the last release before that fix — and exposes a
//! mutable `#[pyclass]` whose borrow flag is exercised from several threads.
//! There is nothing here for a lint to see: the bug was inside PyO3.

use pyo3::prelude::*;

#[pyclass]
struct Counter {
    value: u64,
}

#[pymethods]
impl Counter {
    #[new]
    fn new() -> Self {
        Counter { value: 0 }
    }

    /// `&mut self` goes through the runtime borrow checker on every call.
    fn bump(&mut self) -> u64 {
        self.value += 1;
        self.value
    }

    fn get(&self) -> u64 {
        self.value
    }
}

#[pymodule]
fn pyo3_4948_borrowflag(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Counter>()
}

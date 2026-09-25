//! FT003 — a snapshot taken under one acquisition, written back under another.
//!
//! `normalize` copies the data under a read lock, releases it, sorts the copy,
//! then writes the copy back under a second acquisition. A `push` that lands in
//! between is overwritten: a lost update. Every access is synchronised, so
//! ThreadSanitizer reports nothing; only the lint can see it.
//!
//! The shape every true FT003 positive has had so far: a read-only-looking
//! method snapshots, does its work with the lock released, and writes the
//! stale copy back.

use pyo3::prelude::*;
use std::sync::RwLock;

#[pyclass]
struct Samples {
    data: RwLock<Vec<u64>>,
}

#[pymethods]
impl Samples {
    #[new]
    fn new() -> Self {
        Samples {
            data: RwLock::new(Vec::new()),
        }
    }

    fn push(&self, value: u64) {
        self.data.write().unwrap().push(value);
    }

    fn normalize(&self) {
        let mut copy = self.data.read().unwrap().clone();
        copy.sort_unstable();
        *self.data.write().unwrap() = copy;
    }

    fn len(&self) -> usize {
        self.data.read().unwrap().len()
    }
}

#[pymodule]
fn ft003_snapshot_writeback(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Samples>()
}

//! FT003 — two locks, one invariant, no atomicity.
//!
//! Every individual access is correctly synchronised, so ThreadSanitizer sees
//! nothing wrong: there is no unsynchronised memory access anywhere. The bug is
//! that `count` and `items.len()` are assumed to agree, and nothing holds both
//! locks at once to make that true.

use pyo3::prelude::*;
use std::sync::Mutex;

#[pyclass]
struct Ledger {
    items: Mutex<Vec<u64>>,
    count: Mutex<usize>,
}

#[pymethods]
impl Ledger {
    #[new]
    fn new() -> Self {
        Ledger {
            items: Mutex::new(Vec::new()),
            count: Mutex::new(0),
        }
    }

    fn push(&self, value: u64) {
        self.items.lock().unwrap().push(value);
        // Another thread observing between these two statements sees
        // items.len() != count.
        *self.count.lock().unwrap() += 1;
    }

    fn consistent(&self) -> bool {
        let counted = *self.count.lock().unwrap();
        let actual = self.items.lock().unwrap().len();
        counted == actual
    }
}

#[pymodule]
fn ft003_lock_pair(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Ledger>()
}

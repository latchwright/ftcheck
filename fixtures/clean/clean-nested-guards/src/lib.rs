//! Correct: two locks held at once, always in the same order.
//!
//! `total` holds the first guard while taking the second, so both values are
//! read inside one critical section — nested, not sequential. The lint must not
//! call it a non-atomic sequence. Reconstructed from the most common false
//! positive in the public-project triage.

use pyo3::prelude::*;
use std::sync::Mutex;

#[pyclass]
struct Pair {
    left: Mutex<u64>,
    right: Mutex<u64>,
}

#[pymethods]
impl Pair {
    #[new]
    fn new() -> Self {
        Pair {
            left: Mutex::new(0),
            right: Mutex::new(0),
        }
    }

    /// Both guards live to the end of the block: one atomic update.
    fn bump_both(&self) {
        let mut left = self.left.lock().unwrap();
        let mut right = self.right.lock().unwrap();
        *left += 1;
        *right += 1;
    }

    /// Same order as `bump_both`, so no lock-order inversion either.
    fn total(&self) -> u64 {
        let left = self.left.lock().unwrap();
        let right = self.right.lock().unwrap();
        *left + *right
    }
}

#[pymodule]
fn clean_nested_guards(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Pair>()
}

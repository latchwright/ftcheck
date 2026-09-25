//! A panic that happens only under contention — not a data race.
//!
//! `snapshot` takes an exclusive claim with `try_lock` and `expect`s it to
//! succeed, which is always true on one thread and false as soon as two
//! threads overlap. TSan sees no race: the claim is a proper mutex. The loser
//! gets a Rust panic, surfacing in Python as `PanicException` — a
//! `BaseException`, so `except Exception` does not catch it.
//!
//! The common real-world form: a read-only-looking accessor that takes an
//! exclusive borrow of shared state, so one of two overlapping readers panics.

use pyo3::prelude::*;
use std::sync::Mutex;

#[pyclass]
struct Ledger {
    entries: Mutex<Vec<u32>>,
}

#[pymethods]
impl Ledger {
    #[new]
    fn new() -> Self {
        Ledger {
            entries: Mutex::new((0..64).collect()),
        }
    }

    fn snapshot(&self) -> Vec<u32> {
        let guard = self.entries.try_lock().expect("exclusive claim already taken");
        let copy = guard.clone();
        // Long enough that two threads overlap.
        std::thread::sleep(std::time::Duration::from_micros(50));
        copy
    }
}

#[pymodule]
fn stress_panic_contention(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Ledger>()
}

//! The gap `ftcheck stress` exists for: a race the project's own tests cannot see.
//!
//! `Tally` mutates through an `UnsafeCell` and is declared `Sync` by hand, on
//! the belief that the GIL serialised every call. Concurrent `record` calls on
//! one instance race. The tests below build a fresh `Tally` in every test body,
//! so under `ftcheck ci` each of the N threads has its own instance and nothing
//! is shared — `ci` reports clean. `stress` shares one instance deliberately.
//!
//! The constructor takes a `Settings`, which no primitive satisfies, so the
//! factory has to be declared — see ftcheck.toml. That is the one line a
//! project writes to get this coverage.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::cell::UnsafeCell;

#[pyclass(frozen)]
struct Settings {
    #[pyo3(get)]
    capacity: usize,
}

#[pymethods]
impl Settings {
    #[new]
    fn new(capacity: usize) -> PyResult<Self> {
        if capacity == 0 {
            return Err(PyValueError::new_err("capacity must be positive"));
        }
        Ok(Settings { capacity })
    }
}

#[pyclass(frozen)]
struct Tally {
    counts: UnsafeCell<Vec<u64>>,
}

// The bug: this was true under the GIL and is false without it.
unsafe impl Sync for Tally {}

#[pymethods]
impl Tally {
    #[new]
    fn new(settings: &Settings) -> Self {
        Tally {
            counts: UnsafeCell::new(vec![0; settings.capacity]),
        }
    }

    fn record(&self, slot: usize) {
        let counts = unsafe { &mut *self.counts.get() };
        let n = counts.len();
        counts[slot % n] += 1;
    }

    fn total(&self) -> u64 {
        unsafe { (*self.counts.get()).iter().sum() }
    }
}

#[pymodule]
fn stress_unshared_state(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Settings>()?;
    m.add_class::<Tally>()
}

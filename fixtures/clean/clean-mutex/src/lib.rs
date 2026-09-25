//! Correct: all mutable state lives behind a single mutex, and every invariant
//! this type has is contained within one lock acquisition.

use pyo3::prelude::*;
use std::sync::Mutex;

#[pyclass]
struct Log {
    entries: Mutex<Vec<u64>>,
}

#[pymethods]
impl Log {
    #[new]
    fn new() -> Self {
        Log {
            entries: Mutex::new(Vec::new()),
        }
    }

    fn push(&self, value: u64) {
        self.entries.lock().unwrap().push(value);
    }

    fn len(&self) -> usize {
        self.entries.lock().unwrap().len()
    }
}

#[pymodule]
fn clean_mutex(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Log>()
}

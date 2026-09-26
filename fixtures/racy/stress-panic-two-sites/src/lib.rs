//! Two panic sites with one message — not a data race.
//!
//! `head` and `tail` each take the same exclusive claim with `try_lock` and
//! `expect` it, with the same message. On one thread neither panics; as soon
//! as two threads overlap, each panics at its own line. The two sites print
//! identical messages, so a tool that locates panics by message alone files
//! both under one line and loses the other. Each must be reported at its own
//! line, naming the method that panicked there.

use pyo3::prelude::*;
use std::sync::Mutex;

#[pyclass]
struct Queue {
    items: Mutex<Vec<u32>>,
}

#[pymethods]
impl Queue {
    #[new]
    fn new() -> Self {
        Queue {
            items: Mutex::new((0..64).collect()),
        }
    }

    fn head(&self) -> u32 {
        let guard = self.items.try_lock().expect("queue is busy");
        std::thread::sleep(std::time::Duration::from_micros(50));
        guard[0]
    }

    fn tail(&self) -> u32 {
        let guard = self.items.try_lock().expect("queue is busy");
        std::thread::sleep(std::time::Duration::from_micros(50));
        guard[guard.len() - 1]
    }
}

#[pymodule]
fn stress_panic_two_sites(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Queue>()
}

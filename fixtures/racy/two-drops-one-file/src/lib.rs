//! Racy: two different races in one file under one inlined name.
//!
//! Each `Drop` impl below does an unsynchronised read-modify-write of its own
//! counter, shared by every thread. Inlined, both are symbolised as `drop` in
//! this file, so only their lines tell the two races apart: both must be
//! reported, each at its own line.

use pyo3::prelude::*;

static mut ADMITTED: u64 = 0;
static mut RELEASED: u64 = 0;

struct Admit(u64);

impl Drop for Admit {
    fn drop(&mut self) {
        unsafe { ADMITTED += self.0 };
    }
}

struct Release(u64);

impl Drop for Release {
    fn drop(&mut self) {
        unsafe { RELEASED += self.0 };
    }
}

#[pyfunction]
fn admit(n: usize) -> usize {
    for _ in 0..n {
        drop(Admit(1));
    }
    n
}

#[pyfunction]
fn release(n: usize) -> usize {
    for _ in 0..n {
        drop(Release(1));
    }
    n
}

#[pymodule]
fn two_drops_one_file(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(admit, m)?)?;
    m.add_function(wrap_pyfunction!(release, m)?)
}

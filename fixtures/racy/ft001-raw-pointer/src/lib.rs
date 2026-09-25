//! FT001 — raw-pointer mutation reachable from Python.
//!
//! `#[pyclass]` requires `Sync`, and `rustc` enforces it. This type reaches
//! `Sync` by assertion rather than by construction, which is precisely the
//! escape hatch that compiles cleanly and races at runtime.

use pyo3::prelude::*;

#[pyclass]
struct Buffer {
    ptr: *mut u64,
    len: usize,
}

// SAFETY: asserted, not established. That is the bug.
unsafe impl Send for Buffer {}
unsafe impl Sync for Buffer {}

#[pymethods]
impl Buffer {
    #[new]
    fn new(len: usize) -> Self {
        let mut cells = vec![0u64; len].into_boxed_slice();
        let ptr = cells.as_mut_ptr();
        std::mem::forget(cells);
        Buffer { ptr, len }
    }

    fn bump(&self, index: usize) -> PyResult<u64> {
        if index >= self.len {
            return Err(pyo3::exceptions::PyIndexError::new_err("index out of range"));
        }
        unsafe {
            let slot = self.ptr.add(index);
            *slot += 1;
            Ok(*slot)
        }
    }
}

#[pymodule]
fn ft001_raw_pointer(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Buffer>()
}

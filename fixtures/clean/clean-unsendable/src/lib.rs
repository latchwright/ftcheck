//! Correct: a raw pointer in a `#[pyclass(unsendable)]` type.
//!
//! PyO3 checks on every access that an `unsendable` object is used only from
//! the thread that created it, and raises on any other. No second thread can
//! reach the pointer, so dereferencing it is not a free-threading hazard.
//! Reconstructed from a lint false positive on a public project.

use pyo3::prelude::*;

#[pyclass(unsendable)]
struct Cursor {
    ptr: *mut u64,
}

#[pymethods]
impl Cursor {
    #[new]
    fn new() -> Self {
        Cursor {
            ptr: Box::into_raw(Box::new(0)),
        }
    }

    fn advance(&self) -> u64 {
        unsafe {
            *self.ptr += 1;
            *self.ptr
        }
    }
}

impl Drop for Cursor {
    fn drop(&mut self) {
        unsafe { drop(Box::from_raw(self.ptr)) }
    }
}

#[pymodule]
fn clean_unsendable(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Cursor>()
}

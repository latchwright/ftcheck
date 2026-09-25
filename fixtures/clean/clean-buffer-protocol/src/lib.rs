//! Correct: the buffer protocol's `view` is owned by the caller.
//!
//! CPython hands `__getbuffer__` a fresh `Py_buffer` per request, owned by the
//! requester, and hands the same one back to `__releasebuffer__`. Writing
//! through it is not writing to shared state. The type itself is frozen and
//! immutable. Reconstructed from a lint false positive on a public project,
//! following PyO3's buffer-protocol test.

use pyo3::prelude::*;
use std::os::raw::{c_int, c_void};

#[pyclass(frozen)]
struct Frame {
    data: Vec<u32>,
}

#[pymethods]
impl Frame {
    #[new]
    fn new(len: usize) -> Self {
        Frame {
            data: (0..len as u32).collect(),
        }
    }

    unsafe fn __getbuffer__(
        slf: Bound<'_, Self>,
        view: *mut pyo3::ffi::Py_buffer,
        flags: c_int,
    ) -> PyResult<()> {
        if view.is_null() {
            return Err(pyo3::exceptions::PyBufferError::new_err("view is null"));
        }
        if (flags & pyo3::ffi::PyBUF_WRITABLE) == pyo3::ffi::PyBUF_WRITABLE {
            return Err(pyo3::exceptions::PyBufferError::new_err("not writable"));
        }
        unsafe {
            let view_ref = &mut *view;
            view_ref.obj = slf.clone().into_any().into_ptr();
            let data = &slf.get().data;
            view_ref.buf = data.as_ptr() as *mut c_void;
            view_ref.len = (data.len() * std::mem::size_of::<u32>()) as isize;
            view_ref.readonly = 1;
            view_ref.itemsize = std::mem::size_of::<u32>() as isize;
            view_ref.format = std::ptr::null_mut();
            view_ref.ndim = 1;
            view_ref.shape = std::ptr::null_mut();
            view_ref.strides = std::ptr::null_mut();
            view_ref.suboffsets = std::ptr::null_mut();
            view_ref.internal = std::ptr::null_mut();
        }
        Ok(())
    }

    unsafe fn __releasebuffer__(&self, view: *mut pyo3::ffi::Py_buffer) {
        unsafe {
            let view_ref = &mut *view;
            view_ref.internal = std::ptr::null_mut();
        }
    }

    fn total(&self) -> u64 {
        self.data.iter().map(|&x| x as u64).sum()
    }
}

#[pymodule]
fn clean_buffer_protocol(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Frame>()
}

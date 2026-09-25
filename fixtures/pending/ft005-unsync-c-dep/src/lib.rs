//! FT005 — unsynchronised use of a C dependency that is not thread-safe.

use pyo3::prelude::*;
use std::ffi::CStr;
use std::os::raw::{c_char, c_ulong};

extern "C" {
    fn legacy_format(value: c_ulong) -> *const c_char;
}

#[pyfunction]
fn format_value(value: u64) -> String {
    unsafe {
        let ptr = legacy_format(value as c_ulong);
        CStr::from_ptr(ptr).to_string_lossy().into_owned()
    }
}

#[pymodule]
fn ft005_unsync_c_dep(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(format_value, m)?)
}

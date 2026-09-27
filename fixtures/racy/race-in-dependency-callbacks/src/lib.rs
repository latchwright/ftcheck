//! Racy: the extension's own code, run by a dependency, races.
//!
//! ftcheck suppresses the reports that `oneshot`, `crossbeam-epoch` and glibc's
//! thread teardown produce under ThreadSanitizer (see `clean-dependency-fences`).
//! Each of those dependencies also calls back into the extension: `oneshot`
//! drops an undelivered message, the epoch collector runs deferred closures,
//! and thread exit runs thread-local destructors. The code in those callbacks
//! is the extension's, so a race in it must still be reported, even though
//! every stack it appears on passes through the dependency.
//!
//! Each callback below does an unsynchronised read-modify-write of a counter
//! shared by every thread.

use crossbeam_epoch as epoch;
use pyo3::prelude::*;
use std::thread;

static mut DROPPED: u64 = 0;
static mut RECLAIMED: u64 = 0;
static mut RETIRED: u64 = 0;

/// Dropped by `oneshot` itself when the receiver goes away first.
struct Receipt([u64; 32]);

// Both `Drop` impls in this file are symbolised as `drop`: only their lines
// tell the two races apart.
impl Drop for Receipt {
    fn drop(&mut self) {
        unsafe { DROPPED += self.0[0] };
    }
}

#[pyfunction]
fn undelivered(n: usize) -> usize {
    for _ in 0..n {
        let (tx, rx) = oneshot::channel::<Receipt>();
        tx.send(Receipt([1; 32])).unwrap();
        drop(rx);
    }
    n
}

#[pyfunction]
fn deferred(n: usize) -> usize {
    for _ in 0..n {
        let guard = epoch::pin();
        guard.defer(|| unsafe { RECLAIMED += 1 });
        guard.flush();
    }
    n
}

struct Retire;

impl Drop for Retire {
    fn drop(&mut self) {
        unsafe { RETIRED += 1 };
    }
}

thread_local! {
    static ON_EXIT: Retire = const { Retire };
}

#[pyfunction]
fn thread_exit(py: Python<'_>, n: usize) -> usize {
    py.detach(|| {
        let workers: Vec<_> = (0..n).map(|_| thread::spawn(|| ON_EXIT.with(|_| ()))).collect();
        for w in workers {
            w.join().unwrap();
        }
    });
    n
}

#[pymodule]
fn race_in_dependency_callbacks(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(undelivered, m)?)?;
    m.add_function(wrap_pyfunction!(deferred, m)?)?;
    m.add_function(wrap_pyfunction!(thread_exit, m)?)
}

//! Correct: three patterns whose synchronisation ThreadSanitizer cannot see.
//!
//! Every access below is ordered, but not in a way TSan models, so without
//! ftcheck's default suppressions each one is reported as a data race:
//!
//! - `oneshot` hands the message over with a relaxed load of the channel state
//!   followed by a standalone `fence(Acquire)`. TSan does not model standalone
//!   fences, so the sender's write of the message and the receiver's read (and
//!   its free of the channel) look unordered.
//! - `crossbeam-epoch` reclaims memory once every pinned thread has moved past
//!   an epoch, which it proves with `SeqCst` fences. TSan sees the collector
//!   free a garbage bag, or a finished thread's `Local`, that another thread
//!   read without a happens-before edge.
//! - glibc frees a finished, detached thread's TLS block in `_dl_deallocate_tls`,
//!   on the thread that drops the handle. glibc is not instrumented, so nothing
//!   TSan sees orders the finished thread's own thread-local accesses before
//!   that free.
//!
//! None of it is the extension's code. Each call works on its own data.

use crossbeam_epoch::{self as epoch, Atomic, Owned};
use pyo3::prelude::*;
use std::cell::RefCell;
use std::sync::atomic::Ordering::AcqRel;
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::Duration;

/// Large enough that moving it through the channel is a `memcpy`.
type Payload = [u64; 32];

/// A worker thread sends one value back; the caller polls, then blocks.
#[pyfunction]
fn oneshot_roundtrip(py: Python<'_>, n: usize) -> u64 {
    py.detach(|| {
        let mut total = 0;
        for i in 0..n {
            let (tx, rx) = oneshot::channel::<Payload>();
            let worker = thread::spawn(move || tx.send([i as u64; 32]).unwrap());
            let value = if i % 2 == 0 {
                // try_recv: relaxed load, then fence(Acquire) once it sees MESSAGE.
                loop {
                    match rx.try_recv() {
                        Ok(v) => break v,
                        Err(oneshot::TryRecvError::Empty) => thread::yield_now(),
                        Err(e) => panic!("{e}"),
                    }
                }
            } else {
                rx.recv().unwrap()
            };
            worker.join().unwrap();
            total += value[31];
        }
        total
    })
}

/// Threads swap a shared pointer and retire the old value through the epoch
/// collector, then exit, so their `Local`s are reclaimed by whoever collects.
#[pyfunction]
fn epoch_churn(py: Python<'_>, n: usize) -> usize {
    py.detach(|| {
        let shared = Arc::new(Atomic::new([0u64; 8]));
        let workers: Vec<_> = (0..n)
            .map(|t| {
                let shared = Arc::clone(&shared);
                thread::spawn(move || {
                    for i in 0..64u64 {
                        let guard = epoch::pin();
                        let old = shared.swap(Owned::new([t as u64 ^ i; 8]), AcqRel, &guard);
                        // SAFETY: `old` is unlinked, and only reachable by
                        // threads pinned before the swap.
                        unsafe { guard.defer_destroy(old) };
                    }
                    epoch::pin().flush();
                })
            })
            .collect();
        let count = workers.len();
        for w in workers {
            w.join().unwrap();
        }
        let guard = epoch::pin();
        // SAFETY: every worker has been joined; nothing else holds the pointer.
        unsafe { drop(shared.swap(epoch::Shared::null(), AcqRel, &guard).into_owned()) };
        count
    })
}

thread_local! {
    static SCRATCH: RefCell<Vec<u64>> = const { RefCell::new(Vec::new()) };
}

/// Short-lived threads that use a thread-local with a destructor. Each handle is
/// dropped once its thread has finished, which detaches it: glibc then frees the
/// finished thread's TLS block here, in `_dl_deallocate_tls`.
#[pyfunction]
fn short_lived_threads(py: Python<'_>, n: usize) -> u64 {
    py.detach(|| {
        let (done, finished) = mpsc::channel();
        let workers: Vec<_> = (0..n)
            .map(|i| {
                let done = done.clone();
                thread::spawn(move || {
                    let sum = SCRATCH.with(|s| {
                        s.borrow_mut().push(i as u64);
                        s.borrow().iter().sum::<u64>()
                    });
                    done.send(sum).unwrap();
                })
            })
            .collect();
        let total = (0..n).map(|_| finished.recv().unwrap()).sum();
        // Give each thread time to run its TLS destructors and exit.
        thread::sleep(Duration::from_millis(20));
        drop(workers);
        total
    })
}

#[pymodule]
fn clean_dependency_fences(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(oneshot_roundtrip, m)?)?;
    m.add_function(wrap_pyfunction!(epoch_churn, m)?)?;
    m.add_function(wrap_pyfunction!(short_lived_threads, m)?)
}

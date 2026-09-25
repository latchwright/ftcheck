//! Correct: parallel work on rayon's global pool, called from many Python threads.
//!
//! rayon's job queue is crossbeam-deque, whose buffer reads and writes race by
//! design (documented in its source as "technically speaking a data race [...]
//! as a hack, we use a volatile write"). Without ftcheck's default suppression
//! TSan reports it on any project using rayon — it did, on two public ones.
//! This fixture pins that the suppression holds, and that nothing else here
//! races: each call works on its own data.

use pyo3::prelude::*;
use rayon::prelude::*;

#[pyfunction]
fn parallel_sum(n: u64) -> u64 {
    (0..n).into_par_iter().map(|x| x % 7).sum()
}

#[pyfunction]
fn parallel_sort(n: u32) -> Vec<u32> {
    let mut v: Vec<u32> = (0..n).rev().collect();
    v.par_sort_unstable();
    v
}

#[pymodule]
fn clean_rayon(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parallel_sum, m)?)?;
    m.add_function(wrap_pyfunction!(parallel_sort, m)?)
}

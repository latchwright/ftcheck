"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once.

A contended `&mut self` borrow raises "Already borrowed" — that is PyO3's
runtime borrow checker working. The race is inside that checker on < 0.24.0.
"""
import pyo3_4948_borrowflag as m

SHARED = m.Counter()


def test_bump_one_shared_counter():
    for _ in range(1000):
        try:
            SHARED.bump()
        except RuntimeError:
            pass
        SHARED.get()

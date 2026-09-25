"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once.

Lost updates are possible here but not asserted: the signal is the lint's, and
ThreadSanitizer must stay silent because every access is locked.
"""
import ft003_snapshot_writeback as m

SHARED = m.Samples()


def test_push_and_normalize_one_shared_series():
    for i in range(500):
        SHARED.push(i)
        SHARED.normalize()
        SHARED.len()

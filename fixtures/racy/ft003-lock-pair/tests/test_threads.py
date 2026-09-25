"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once.

Every access is behind a mutex, so ThreadSanitizer must stay silent. The
invariant `consistent()` checks can still be observed broken — that is the
logic race only the lint can see — so it is exercised but deliberately not
asserted.
"""
import ft003_lock_pair as m

SHARED = m.Ledger()


def test_push_and_check_one_shared_ledger():
    for i in range(1000):
        SHARED.push(i)
        SHARED.consistent()

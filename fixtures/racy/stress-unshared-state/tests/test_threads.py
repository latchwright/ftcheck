"""A typical suite: every test builds its own object.

Under `ftcheck ci` each test body runs in N threads, but each thread builds its
own `Tally`, so no instance is ever shared and ThreadSanitizer has nothing to
see. This is the common shape of real test suites, and why `ci` alone is not
enough.
"""
import stress_unshared_state as m


def test_record_and_total():
    tally = m.Tally(m.Settings(4))
    for i in range(1000):
        tally.record(i)
    assert tally.total() == 1000

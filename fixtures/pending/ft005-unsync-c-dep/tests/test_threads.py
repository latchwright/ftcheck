"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once.

The result is not asserted: under the race it may be another thread's value.
"""
import ft005_unsync_c_dep as m


def test_format_from_many_threads():
    for i in range(1000):
        m.format_value(i)

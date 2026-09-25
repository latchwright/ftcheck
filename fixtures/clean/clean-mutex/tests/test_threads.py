"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_mutex as m

SHARED = m.Log()


def test_push_to_one_shared_log():
    for i in range(1000):
        SHARED.push(i)
        SHARED.len()

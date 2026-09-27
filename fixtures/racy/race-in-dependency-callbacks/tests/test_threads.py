"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import race_in_dependency_callbacks as m


def test_callbacks_from_many_threads():
    for _ in range(20):
        assert m.undelivered(50) == 50
        assert m.deferred(50) == 50
        assert m.thread_exit(2) == 2

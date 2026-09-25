"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_atomics as m


def test_bump_and_name_from_many_threads():
    for _ in range(1000):
        m.bump()
        assert m.name() == "ftcheck"

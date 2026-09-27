"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import two_drops_one_file as m


def test_admit_and_release_from_many_threads():
    for _ in range(200):
        m.admit(5)
        m.release(5)

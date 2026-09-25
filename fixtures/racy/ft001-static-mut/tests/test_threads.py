"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import ft001_static_mut as m


def test_bump_from_many_threads():
    for _ in range(1000):
        m.bump()

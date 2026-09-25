"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import ft001_raw_pointer as m

SHARED = m.Buffer(4)


def test_bump_one_shared_buffer():
    for i in range(1000):
        SHARED.bump(i % 4)

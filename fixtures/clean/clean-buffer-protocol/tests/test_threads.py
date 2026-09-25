"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_buffer_protocol as m

SHARED = m.Frame(8)


def test_many_views_of_one_shared_frame():
    for _ in range(500):
        with memoryview(SHARED) as view:
            assert view.nbytes == 32
        assert SHARED.total() == 28

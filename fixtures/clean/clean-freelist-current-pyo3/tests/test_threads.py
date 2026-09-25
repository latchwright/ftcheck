"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_freelist_current_pyo3 as m


def test_allocate_and_free_through_the_freelist():
    for i in range(2000):
        assert m.Token(i).id == i

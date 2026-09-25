"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_frozen as m

SHARED = m.Label("shared")


def test_read_one_shared_frozen_instance():
    for _ in range(1000):
        assert SHARED.text() == "shared"

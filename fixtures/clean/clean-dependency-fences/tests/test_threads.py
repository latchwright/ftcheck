"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_dependency_fences as m


def test_dependency_synchronisation_from_many_threads():
    for _ in range(5):
        assert m.oneshot_roundtrip(8) == sum(range(8))
        assert m.epoch_churn(4) == 4
        assert m.short_lived_threads(4) == sum(range(4))

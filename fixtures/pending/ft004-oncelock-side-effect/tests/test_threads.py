"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once.

Every access is atomic, so ThreadSanitizer must stay silent even though the
initialiser's side effect may run more than once.
"""
import ft004_oncelock_side_effect as m


def test_config_from_many_threads():
    for _ in range(200):
        assert m.config() == "default"
    assert m.init_count() >= 1

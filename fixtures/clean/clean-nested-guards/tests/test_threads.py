"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_nested_guards as m

SHARED = m.Pair()


def test_bump_and_total_one_shared_pair():
    for _ in range(500):
        SHARED.bump_both()
        assert SHARED.total() % 2 == 0, "both halves always move together"

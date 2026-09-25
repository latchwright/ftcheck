"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once."""
import clean_rayon as m


def test_parallel_work_from_many_threads():
    for _ in range(50):
        assert m.parallel_sum(10_000) == sum(x % 7 for x in range(10_000))
        assert m.parallel_sort(2_000)[:3] == [0, 1, 2]

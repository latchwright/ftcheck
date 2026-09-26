"""Each test builds its own object, so under `ftcheck ci` no instance is
shared and neither site panics."""
import stress_panic_two_sites as m


def test_head_and_tail():
    queue = m.Queue()
    for _ in range(100):
        assert queue.head() == 0
        assert queue.tail() == 63

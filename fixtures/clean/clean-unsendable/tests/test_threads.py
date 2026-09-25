"""Run by `ftcheck ci` under pytest-run-parallel: this body runs in N threads at once.

Each thread builds its own `Cursor`: an unsendable object shared across threads
raises by design, which would be PyO3 working, not a race.
"""
import clean_unsendable as m


def test_advance_a_thread_confined_cursor():
    cursor = m.Cursor()
    for i in range(1, 1001):
        assert cursor.advance() == i

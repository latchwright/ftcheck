"""A typical suite: each test builds its own object, so under `ftcheck ci`
no instance is shared and the panic never happens."""
import stress_panic_contention as m


def test_snapshot():
    ledger = m.Ledger()
    for _ in range(100):
        assert len(ledger.snapshot()) == 64

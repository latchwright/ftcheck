# Ground truth

Every fixture is a minimal PyO3 crate plus an `expected.toml` stating what a correct
ftcheck must say about it.

**One format, two consumers.** The same file drives the static lint suite
(`tests/test_ground_truth.py`) and the ThreadSanitizer suite
(`tests/test_tsan_ground_truth.py`). Ground truth kept in two places is ground truth that
can drift apart while both halves still pass their own tests.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `race` | yes | Whether **ThreadSanitizer** must report a data race |
| `rules` | yes | The exact rule ids the **lint** must emit. Order-insensitive |
| `justification` | yes | Why this is ground truth. Empty is rejected |
| `symbols` | no | Substrings that must appear in a reported stack or location |
| `pending` | no | Ground truth exists, the lint rule does not yet |
| `tsan_pending` | no | Why the sanitizer half cannot run yet. A reason, not a flag |
| `ci_race` | no | Whether `ci`, running the fixture's own tests, must see the race. Defaults to `race` |
| `stress_rules` | no | Non-TSan findings `stress` must report: `stress/panic`, `stress/hang` |
| `pyo3_version` | no | The PyO3 version the expectation depends on |

An unknown field is an error, not a silent default — `pendign = true` must fail loudly
rather than quietly disable an assertion.

## `race` and `rules` are independent

They answer different questions, and a fixture may set one without the other:

- **`race = true`, `rules = []`** — a real data race no static rule can see. TSan finds
  it; the lint is not expected to.
- **`race = false`, `rules = ["FT003"]`** — a *logic* race: two locks, a broken
  invariant, no unsynchronised memory access. TSan will never report it. Recording this
  stops a later contributor "fixing" the sanitizer suite to expect a report that cannot
  come.

## `pending`

Ground truth should outrun the implementation rather than trail it. A `pending` fixture is
reported as an expected failure and **never silently skipped**, so the gap stays visible.

## `tests/` — the fixture's own suite

`ftcheck ci` runs a project's own tests, so every fixture has one. Each test body runs
in N threads at once under pytest-run-parallel, so state the test touches at module
level is shared across threads. Tests exercise the racy path but never assert on a value
the race can corrupt — a test failure is not the signal, a TSan report is.

## `tsan_pending`

Some fixtures cannot be run under the sanitizer for reasons outside this repository —
today, PyO3 0.23 cannot target 3.14 and the upstream 3.13t image is unavailable. The
reason is recorded in the fixture, reported as an expected failure, and rechecked on
its own terms.

## Clean fixtures carry the same weight

A finding on a clean fixture fails the suite exactly as hard as a miss on a racy one.
A correctness tool that cries wolf is worse than one that stays quiet.

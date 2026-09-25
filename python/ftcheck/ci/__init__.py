# SPDX-License-Identifier: MIT OR Apache-2.0
"""`ftcheck ci` — the ThreadSanitizer pipeline.

Consumes a TSan-instrumented free-threaded CPython (it does not build one) and
adds the Rust layer: nightly with `rust-src`, `-Zsanitizer=thread` and
`-Zbuild-std`, C dependencies instrumented by the matching clang, and the
project's suite run concurrently under pytest-run-parallel.
"""
from __future__ import annotations

from dataclasses import dataclass

from ftcheck.ci.environment import Environment
from ftcheck.ci.pipeline import Outcome
from ftcheck.exit_codes import CLEAN, FINDINGS, SUITE_FAILED, UNAVAILABLE

__all__ = ["Verdict", "describe_findings", "verdict"]


def describe_findings(findings: list[dict]) -> str:
    """"2 ThreadSanitizer reports and 1 panic under concurrency in your extension"."""
    kinds = [
        ("tsan/", "ThreadSanitizer report", "ThreadSanitizer reports"),
        ("stress/panic", "panic under concurrency", "panics under concurrency"),
        ("stress/hang", "hang under concurrency", "hangs under concurrency"),
    ]
    parts = []
    for prefix, one, many in kinds:
        n = sum(1 for f in findings if f["rule"].startswith(prefix))
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    other = len(findings) - sum(int(p.split()[0]) for p in parts)
    if other:
        parts.append(f"{other} other finding{'s' if other != 1 else ''}")
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{joined} in your extension"

# pytest exit statuses that mean the suite itself never ran properly.
_PYTEST_BROKEN = {2: "interrupted", 3: "internal error", 4: "usage error", 5: "no tests collected"}


@dataclass(frozen=True)
class Verdict:
    code: int
    headline: str


def verdict(env: Environment, outcome: Outcome | None) -> Verdict:
    """The exit code, and the one sentence that justifies it.

    Order matters. Anything that means the run did not really happen is
    checked before findings are counted, and "clean" is what is left when
    every other explanation has been ruled out — never the default.
    """
    if not env.ok:
        names = ", ".join(c.name for c in env.failures)
        return Verdict(UNAVAILABLE, f"could not run: the environment failed preflight ({names})")
    if outcome is None or outcome.stage != "done":
        stage = outcome.stage if outcome else "preflight"
        why = outcome.error if outcome and outcome.error else "unknown failure"
        return Verdict(UNAVAILABLE, f"could not run: {why} (stage: {stage})")
    if outcome.findings:
        return Verdict(FINDINGS, describe_findings(outcome.findings))
    # After a confirmed finding, a TSan abort (often a heap the race already
    # corrupted) does not undo it; before any finding, it means nothing ran.
    if outcome.fatal:
        return Verdict(UNAVAILABLE, "could not run: ThreadSanitizer aborted — " + outcome.fatal[0])
    if getattr(outcome, "aborted", None):
        return Verdict(UNAVAILABLE, f"could not run: {outcome.aborted}; see the log")
    if outcome.pytest_exit in _PYTEST_BROKEN:
        return Verdict(
            UNAVAILABLE,
            f"could not run: pytest {_PYTEST_BROKEN[outcome.pytest_exit]} "
            f"(exit {outcome.pytest_exit}); nothing was exercised",
        )
    if outcome.tests_collected == 0:
        return Verdict(UNAVAILABLE, "could not run: no tests were executed; nothing was exercised")
    if outcome.pytest_exit not in (0, None) or outcome.tests_failed or outcome.tests_errored:
        # A failing suite is a real failure, but not a race ftcheck observed:
        # its own exit code, so CI can tell the two apart (a first user could not).
        return Verdict(
            SUITE_FAILED,
            f"the test suite failed under the instrumented build "
            f"({outcome.tests_failed} failed, {outcome.tests_errored} errors); "
            "ThreadSanitizer reported no race in your extension. Tests that are not "
            "thread-safe can be marked @pytest.mark.thread_unsafe",
        )
    return Verdict(CLEAN, "no race detected on the exercised surface")

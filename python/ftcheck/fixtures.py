# SPDX-License-Identifier: MIT OR Apache-2.0
"""The ground-truth contract.

Every fixture carries one `expected.toml` stating what a correct tool must say
about it. The same file feeds the lint suite and the sanitizer suite.
Ground truth kept in two places is ground truth that can drift apart while both
halves still pass their own tests.
"""
from __future__ import annotations

import pathlib
import tomllib
from dataclasses import dataclass, field

__all__ = ["Expectation", "discover", "load_expectation"]

_REQUIRED = ("race", "rules", "justification")


@dataclass(frozen=True)
class Expectation:
    """What a correct ftcheck must report about one fixture.

    Attributes:
        race: whether ThreadSanitizer must report a data race. A logic race
            (two locks, broken invariant) sets this False while still listing
            rules — TSan cannot see it, and recording that stops a later
            contributor "fixing" the sanitizer suite to expect a report that
            will never come.
        rules: the exact rule ids the lint must emit. Order-insensitive.
        symbols: substrings that must appear in a reported stack or location.
        justification: why this is ground truth. Mandatory.
        pending: ground truth exists, the lint rule does not yet. Reported as
            an expected failure, never silently skipped.
        ci_race: whether `ftcheck ci`, running the fixture's own tests, must
            see the race. Defaults to `race`. False marks a race only
            `ftcheck stress` can reach, because the tests never share an
            instance between threads — the gap stress exists to close.
        stress_rules: findings `ftcheck stress` must report that are not TSan
            races — `stress/panic`, `stress/hang`. Order-insensitive.
        tsan_pending: why the sanitizer half of this fixture cannot be run
            yet, or None when it can. A reason, not a flag, because the
            blockers are external and each needs to be rechecked on its own.
        pyo3_version: the PyO3 version this fixture is pinned to, when the
            expectation depends on it.
    """

    race: bool
    rules: list[str]
    justification: str
    symbols: list[str] = field(default_factory=list)
    pending: bool = False
    pyo3_version: str | None = None
    tsan_pending: str | None = None
    ci_race: bool | None = None
    stress_rules: list[str] = field(default_factory=list)

    @property
    def ci_must_race(self) -> bool:
        return self.race if self.ci_race is None else self.ci_race


def load_expectation(path: pathlib.Path) -> Expectation:
    """Parse one `expected.toml`, refusing anything under-specified."""
    data = tomllib.loads(pathlib.Path(path).read_text())

    for key in _REQUIRED:
        if key not in data:
            raise ValueError(f"{path}: missing required field {key!r}")
    if not str(data["justification"]).strip():
        raise ValueError(f"{path}: justification must not be empty")

    unknown = set(data) - {
        "race",
        "rules",
        "symbols",
        "justification",
        "pending",
        "pyo3_version",
        "tsan_pending",
        "ci_race",
        "stress_rules",
    }
    if unknown:
        raise ValueError(f"{path}: unknown field(s) {sorted(unknown)}")

    if data.get("ci_race") and not data["race"]:
        raise ValueError(f"{path}: ci_race cannot be true when race is false")
    tsan_pending = data.get("tsan_pending")
    if tsan_pending is not None and not str(tsan_pending).strip():
        raise ValueError(f"{path}: tsan_pending must give a reason")

    return Expectation(
        race=bool(data["race"]),
        rules=list(data["rules"]),
        justification=str(data["justification"]),
        symbols=list(data.get("symbols", [])),
        pending=bool(data.get("pending", False)),
        pyo3_version=data.get("pyo3_version"),
        tsan_pending=str(tsan_pending) if tsan_pending is not None else None,
        ci_race=bool(data["ci_race"]) if "ci_race" in data else None,
        stress_rules=list(data.get("stress_rules", [])),
    )


def discover(root: pathlib.Path) -> list[tuple[pathlib.Path, Expectation]]:
    """Find every `<root>/<group>/<fixture>/expected.toml`, sorted by path."""
    root = pathlib.Path(root)
    return [(p, load_expectation(p)) for p in sorted(root.glob("*/*/expected.toml"))]

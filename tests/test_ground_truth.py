# SPDX-License-Identifier: MIT OR Apache-2.0
"""Every fixture, checked against what it says a correct tool must report.

A false positive on a clean fixture fails exactly as hard as a miss on a racy
one. That symmetry is the point: a correctness tool that cries wolf is worse
than one that stays quiet.
"""
import pathlib

import pytest
from ftcheck import lint
from ftcheck.fixtures import discover

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = discover(ROOT / "fixtures")


@pytest.mark.parametrize(
    "path,expectation", FIXTURES, ids=[p.parent.name for p, _ in FIXTURES]
)
def test_fixture_matches_ground_truth(path, expectation):
    if expectation.pending:
        pytest.xfail(f"ground truth exists, rule does not: {expectation.rules}")

    report = lint(str(path.parent), min_confidence="likely")
    reported = sorted({f["rule"] for f in report["findings"]})
    assert reported == sorted(expectation.rules), (
        f"{path.parent.name}: expected {sorted(expectation.rules)}, got {reported}. "
        "A false positive on a clean fixture fails as hard as a miss on a racy one."
    )


def test_no_clean_fixture_produces_any_finding():
    for path, expectation in FIXTURES:
        if expectation.race is False and not expectation.rules and not expectation.pending:
            report = lint(str(path.parent), min_confidence="likely")
            assert report["findings"] == [], f"{path.parent.name} produced a false positive"


def test_the_freelist_pair_disagrees_only_on_pyo3_version():
    """The single most important assertion in the suite.

    Both crates carry byte-identical source. If this ever passes for both or
    fails for both, FT002 has regressed to the unconditional form the
    specification originally asked for, which fires on correct code.
    """
    old_dir = ROOT / "fixtures/racy/ft002-freelist-old-pyo3"
    new_dir = ROOT / "fixtures/clean/clean-freelist-current-pyo3"

    assert (old_dir / "src/lib.rs").read_text().split("#[pymodule]")[0] == (
        new_dir / "src/lib.rs"
    ).read_text().split("#[pymodule]")[0], "the pair must share source to prove the point"

    old = lint(str(old_dir))
    new = lint(str(new_dir))
    assert [f["rule"] for f in old["findings"]] == ["FT002"]
    assert new["findings"] == []
    assert old["pyo3_version"] == "0.23.4"
    assert new["pyo3_version"].startswith("0.29")


def test_every_rule_with_a_racy_fixture_also_has_a_clean_one():
    """A rule proven only by what it catches is half proven."""
    racy_rules = {r for _, e in FIXTURES if e.rules and not e.pending for r in e.rules}
    assert racy_rules, "no rules under test"
    clean_fixtures = [
        p for p, e in FIXTURES if not e.rules and e.race is False and not e.pending
    ]
    assert len(clean_fixtures) >= len(racy_rules), (
        f"{len(racy_rules)} rules but only {len(clean_fixtures)} clean fixtures"
    )

# SPDX-License-Identifier: MIT OR Apache-2.0
import pathlib

from ftcheck.fixtures import discover

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_every_committed_fixture_has_a_valid_expectation():
    found = discover(ROOT / "fixtures")
    assert found, "no fixtures discovered"
    for path, exp in found:
        assert exp.justification, f"{path} has no justification"
        if not exp.race and not exp.rules:
            assert not exp.pending, f"{path}: a clean fixture cannot be pending"

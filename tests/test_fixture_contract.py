# SPDX-License-Identifier: MIT OR Apache-2.0
import pathlib

import pytest
from ftcheck.fixtures import load_expectation

ROOT = pathlib.Path(__file__).resolve().parents[1]


def write(tmp_path, body):
    (tmp_path / "expected.toml").write_text(body)
    return tmp_path / "expected.toml"


def test_loads_a_racy_expectation(tmp_path):
    p = write(
        tmp_path,
        'race = true\nrules = ["FT001"]\nsymbols = ["bump"]\n'
        'justification = "static mut mutated from a #[pyfunction]"\n',
    )
    e = load_expectation(p)
    assert e.race is True
    assert e.rules == ["FT001"]
    assert e.pending is False


def test_pending_marks_ground_truth_that_outruns_the_implementation(tmp_path):
    p = write(
        tmp_path,
        'race = true\nrules = ["FT004"]\n'
        'justification = "OnceLock initialiser with an observable side effect"\n'
        "pending = true\n",
    )
    assert load_expectation(p).pending is True


def test_justification_is_mandatory(tmp_path):
    p = write(tmp_path, 'race = true\nrules = ["FT001"]\n')
    with pytest.raises(ValueError, match="justification"):
        load_expectation(p)


def test_empty_justification_is_rejected(tmp_path):
    p = write(tmp_path, 'race = true\nrules = []\njustification = "   "\n')
    with pytest.raises(ValueError, match="must not be empty"):
        load_expectation(p)


def test_a_typo_in_a_field_name_is_an_error_not_a_silent_default(tmp_path):
    p = write(
        tmp_path,
        'race = true\nrules = ["FT001"]\njustification = "x"\npendign = true\n',
    )
    with pytest.raises(ValueError, match="unknown field"):
        load_expectation(p)


def test_tsan_pending_carries_a_reason(tmp_path):
    p = write(
        tmp_path,
        'race = true\nrules = []\njustification = "x"\n'
        'tsan_pending = "no interpreter this PyO3 can target"\n',
    )
    assert load_expectation(p).tsan_pending == "no interpreter this PyO3 can target"


def test_an_empty_tsan_pending_reason_is_rejected(tmp_path):
    p = write(tmp_path, 'race = true\nrules = []\njustification = "x"\ntsan_pending = " "\n')
    with pytest.raises(ValueError, match="tsan_pending"):
        load_expectation(p)


def test_ci_race_defaults_to_race(tmp_path):
    p = write(tmp_path, 'race = true\nrules = []\njustification = "x"\n')
    assert load_expectation(p).ci_must_race is True


def test_ci_race_false_marks_a_stress_only_race(tmp_path):
    p = write(tmp_path, 'race = true\nci_race = false\nrules = []\njustification = "x"\n')
    e = load_expectation(p)
    assert e.race is True and e.ci_must_race is False


def test_ci_cannot_see_a_race_that_does_not_exist(tmp_path):
    p = write(tmp_path, 'race = false\nci_race = true\nrules = []\njustification = "x"\n')
    with pytest.raises(ValueError, match="ci_race"):
        load_expectation(p)

# SPDX-License-Identifier: MIT OR Apache-2.0
"""The exit-code contract, and the language the tool is allowed to use."""
import json
import pathlib
import tomllib

import ftcheck

from ftcheck.cli import main
from ftcheck.exit_codes import CLEAN, FINDINGS, UNAVAILABLE, USAGE

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLEAN_CRATE = str(ROOT / "fixtures/clean/clean-mutex")
RACY_CRATE = str(ROOT / "fixtures/racy/ft001-static-mut")
LOCK_PAIR = str(ROOT / "fixtures/racy/ft003-lock-pair")


def test_version_agrees_everywhere():
    """The wheel, the crates and `ftcheck --version` all carry one version."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    cargo = tomllib.loads((ROOT / "Cargo.toml").read_text())
    assert ftcheck.__version__ == pyproject["project"]["version"]
    assert ftcheck.__version__ == cargo["workspace"]["package"]["version"]


def test_clean_fixture_exits_zero():
    assert main(["lint", CLEAN_CRATE]) == CLEAN


def test_racy_fixture_exits_one():
    assert main(["lint", RACY_CRATE]) == FINDINGS


def test_unknown_flag_exits_two():
    assert main(["lint", CLEAN_CRATE, "--nonsense"]) == USAGE


def test_missing_path_exits_two():
    assert main(["lint", "/does/not/exist"]) == USAGE


def test_bad_confidence_value_exits_two():
    assert main(["lint", CLEAN_CRATE, "--confidence", "extremely"]) == USAGE


def test_no_subcommand_exits_two():
    assert main([]) == USAGE


def test_likely_findings_do_not_fail_by_default():
    assert main(["lint", LOCK_PAIR]) == CLEAN


def test_likely_findings_fail_when_requested():
    assert main(["lint", LOCK_PAIR, "--confidence", "likely"]) == FINDINGS


def test_summary_never_claims_safety(capsys):
    main(["lint", CLEAN_CRATE])
    out = capsys.readouterr().out.lower()
    assert "safe" not in out, "the tool never claims safety, only what it exercised"
    assert "exercised surface" in out


def test_summary_reports_coverage(capsys):
    main(["lint", RACY_CRATE])
    out = capsys.readouterr().out
    assert "scanned 1 file" in out
    assert "1 Python entry point" in out


def test_json_format_round_trips(capsys):
    main(["lint", RACY_CRATE, "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert [f["rule"] for f in report["findings"]] == ["FT001"]
    assert report["files_scanned"] == 1


def test_sarif_and_junit_are_written(tmp_path):
    sarif = tmp_path / "out.sarif"
    junit = tmp_path / "out.xml"
    assert main(["lint", RACY_CRATE, "--sarif", str(sarif), "--junit", str(junit)]) == FINDINGS
    doc = json.loads(sarif.read_text())
    assert doc["version"] == "2.1.0"
    assert doc["runs"][0]["results"][0]["ruleId"] == "FT001"
    assert 'failures="1"' in junit.read_text()


def test_sarif_is_written_even_for_a_clean_run(tmp_path):
    sarif = tmp_path / "clean.sarif"
    assert main(["lint", CLEAN_CRATE, "--sarif", str(sarif)]) == CLEAN
    doc = json.loads(sarif.read_text())
    assert doc["runs"][0]["results"] == [], "a clean run still produces a report"


# --- ftcheck ci --------------------------------------------------------------
# These run on an ordinary interpreter, which is exactly the case that matters:
# outside a sanitizer environment `ci` must refuse with 3, never report clean.


def test_ci_outside_a_tsan_environment_could_not_run(capsys, tmp_path):
    code = main(["ci", CLEAN_CRATE, "--work-dir", str(tmp_path)])
    assert code == UNAVAILABLE
    out = capsys.readouterr().out
    assert "could not run" in out
    assert "tsan_interpreter" in out


def test_ci_check_outside_a_tsan_environment_could_not_run():
    assert main(["ci", CLEAN_CRATE, "--check"]) == UNAVAILABLE


def test_ci_json_carries_the_verdict_and_every_check(capsys, tmp_path):
    main(["ci", CLEAN_CRATE, "--check", "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == UNAVAILABLE
    names = {c["name"] for c in report["environment"]["checks"]}
    assert {"interpreter", "free_threaded", "tsan_interpreter"} <= names


def test_ci_single_thread_is_a_usage_error():
    assert main(["ci", CLEAN_CRATE, "--threads", "1"]) == USAGE


def test_ci_missing_project_is_a_usage_error():
    assert main(["ci", "/does/not/exist"]) == USAGE


def test_ci_missing_suppression_file_is_a_usage_error():
    assert main(["ci", CLEAN_CRATE, "--suppressions", "/does/not/exist"]) == USAGE


def test_ci_refuses_force_seq_cst_atomics(capsys, monkeypatch):
    monkeypatch.setenv("TSAN_OPTIONS", "halt_on_error=0 force_seq_cst_atomics=1")
    main(["ci", CLEAN_CRATE, "--check", "--format", "json"])
    checks = json.loads(capsys.readouterr().out)["environment"]["checks"]
    (tsan_options,) = [c for c in checks if c["name"] == "tsan_options"]
    assert tsan_options["ok"] is False


# --- ftcheck stress ----------------------------------------------------------


def test_stress_outside_a_tsan_environment_could_not_run(capsys, tmp_path):
    assert main(["stress", CLEAN_CRATE, "--work-dir", str(tmp_path), "--seed", "7"]) == UNAVAILABLE
    out = capsys.readouterr().out
    assert "could not run" in out
    line = next(ln for ln in out.splitlines() if "replay:" in ln)
    assert "--replay 7" in line, "the seed is printed even when nothing ran"
    for part in (CLEAN_CRATE, "--threads 8", "--iterations 200", "--budget 120"):
        assert part in line, f"a replay line must be complete: {part}"


def test_stress_seed_and_replay_are_exclusive():
    assert main(["stress", CLEAN_CRATE, "--seed", "1", "--replay", "2"]) == USAGE


def test_stress_rejects_a_non_positive_budget():
    assert main(["stress", CLEAN_CRATE, "--budget", "0"]) == USAGE


def test_stress_rejects_an_unknown_config_key(tmp_path):
    (tmp_path / "ftcheck.toml").write_text('[stress]\nfactorys = {}\n')
    assert main(["stress", str(tmp_path), "--check"]) == USAGE


def test_stress_json_carries_seed_and_coverage(capsys):
    main(["stress", CLEAN_CRATE, "--check", "--replay", "42", "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert report["seed"] == 42
    assert report["coverage"] == {"driven": 0, "total": 0, "confined": 0}


def test_lint_on_a_directory_with_no_rust_source_could_not_run(tmp_path):
    """Found on the corpus run: a lint that scanned 0 files must not read clean."""
    (tmp_path / "README.md").write_text("no crate here\n")
    assert main(["lint", str(tmp_path)]) == UNAVAILABLE



def test_lint_reports_paths_relative_to_the_project(capsys):
    """Inside the container the absolute path is /src/...; a first user saw it."""
    main(["lint", RACY_CRATE, "--format", "json"])
    (finding,) = json.loads(capsys.readouterr().out)["findings"]
    assert finding["primary"]["file"] == "src/lib.rs"

# SPDX-License-Identifier: MIT OR Apache-2.0
"""Stress configuration: where factories come from, and refusing typos."""
import pytest
from ftcheck.stress import coverage, load_config


def test_no_config_is_an_empty_config(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg["factories"] == {} and cfg["modules"] == [] and cfg["source"] is None


def test_pyproject_table_is_read(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ftcheck.stress]\n'
        'modules = ["pkg._native"]\n'
        '[tool.ftcheck.stress.factories]\n'
        '"pkg._native.Counter" = "pkg._native.Counter(4)"\n'
        '[tool.ftcheck.stress.args]\n'
        '"pkg._native.Counter.add" = ["1", "2"]\n'
        '[tool.ftcheck.stress.skip]\n'
        '"pkg._native.Counter.close" = "invalidates the instance"\n'
    )
    cfg = load_config(tmp_path)
    assert cfg["modules"] == ["pkg._native"]
    assert cfg["factories"]["pkg._native.Counter"] == "pkg._native.Counter(4)"
    assert cfg["args"]["pkg._native.Counter.add"] == ["1", "2"]
    assert "close" in next(iter(cfg["skip"]))


def test_ftcheck_toml_is_read(tmp_path):
    (tmp_path / "ftcheck.toml").write_text('[stress.factories]\n"m.T" = "m.T()"\n')
    assert load_config(tmp_path)["factories"] == {"m.T": "m.T()"}


def test_configuring_both_places_is_an_error(tmp_path):
    (tmp_path / "ftcheck.toml").write_text("[stress]\nmodules = []\n")
    (tmp_path / "pyproject.toml").write_text("[tool.ftcheck.stress]\nmodules = []\n")
    with pytest.raises(ValueError, match="both"):
        load_config(tmp_path)


def test_a_misspelt_key_is_an_error_not_a_silent_default(tmp_path):
    (tmp_path / "ftcheck.toml").write_text("[stress]\nfactorys = {}\n")
    with pytest.raises(ValueError, match="unknown"):
        load_config(tmp_path)


def test_a_missing_factories_file_is_an_error(tmp_path):
    (tmp_path / "ftcheck.toml").write_text('[stress]\nfactories_file = "nope.py"\n')
    with pytest.raises(ValueError, match="does not exist"):
        load_config(tmp_path)


def test_coverage_counts_only_groups_that_were_actually_driven():
    result = {
        "groups": [
            {"driven": ["m.A.f", "m.A.g"], "not_driven": {"m.A.h": "x"}, "pairs_driven": 3},
            {"driven": ["m.B.f"], "not_driven": {}, "pairs_driven": 0},
        ]
    }
    assert coverage(result) == (2, 4)


def test_a_panic_only_under_concurrency_is_a_finding():
    from ftcheck.stress import panic_findings

    result = {
        "groups": [
            {
                "driven": ["m.Enc.ids", "m.Enc.bad_input"],
                "serial_exceptions": {"m.Enc.ids": [], "m.Enc.bad_input": ["PanicException"]},
            }
        ],
        "calls": {"m.Enc.ids": 100, "m.Enc.bad_input": 100},
        "exceptions": {
            "m.Enc.ids": {"PanicException": 14},
            "m.Enc.bad_input": {"PanicException": 100},
        },
        "messages": {"m.Enc.ids": {"PanicException": "already borrowed"}},
    }
    (finding,) = panic_findings(result, threads=8, seed=3)
    assert finding["rule"] == "stress/panic"
    assert finding["symbol"] == "m.Enc.ids", "a panic the baseline also raised is not a finding"
    assert "14 of 100" in finding["message"] and "already borrowed" in finding["message"]
    assert "--replay 3" in finding["message"]


def test_every_default_suppression_is_narrow_and_justified():
    """race_top only, each preceded by a comment: a broad `race:` would hide real
    user races that run inside a dependency."""
    from ftcheck.ci.pipeline import DEFAULT_SUPPRESSIONS

    lines = DEFAULT_SUPPRESSIONS.read_text().splitlines()
    entries = [(i, ln) for i, ln in enumerate(lines) if ln and not ln.startswith("#")]
    assert entries, "the default file must not be empty"
    for i, line in entries:
        assert line.startswith("race_top:"), line
        assert any(lines[j].startswith("#") for j in range(max(0, i - 3), i)), line



def test_panics_at_one_location_are_one_finding_located_there():
    from ftcheck.stress import panic_findings, panic_locations

    log = (
        "thread '<unnamed>' panicked at src/lib.rs:40:17:\n"
        "Already mutably borrowed\n"
        "note: run with `RUST_BACKTRACE=1`\n"
    )
    result = {
        "groups": [
            {
                "driven": ["m.store.put", "m.store.get"],
                "serial_exceptions": {"m.store.put": [], "m.store.get": []},
            }
        ],
        "calls": {"m.store.put": 50, "m.store.get": 50},
        "exceptions": {
            "m.store.put": {"PanicException": 3},
            "m.store.get": {"PanicException": 4},
        },
        "messages": {
            "m.store.put": {"PanicException": "Already mutably borrowed"},
            "m.store.get": {"PanicException": "Already mutably borrowed"},
        },
    }
    (finding,) = panic_findings(result, threads=8, seed=1, locations=panic_locations(log))
    assert finding["primary"] == {"file": "src/lib.rs", "line": 40, "column": 17}
    assert "7 of 100 calls" in finding["message"]
    assert "`m.store.put`" in finding["message"] and "`m.store.get`" in finding["message"]


def _two_site_result(panic_threads, **extra):
    """Two callables on one type, both panicking with the same message."""
    return {
        "groups": [
            {
                "driven": ["m.Shelf.left", "m.Shelf.right"],
                "serial_exceptions": {"m.Shelf.left": [], "m.Shelf.right": []},
            }
        ],
        "calls": {"m.Shelf.left": 40, "m.Shelf.right": 60},
        "exceptions": {
            "m.Shelf.left": {"PanicException": 2},
            "m.Shelf.right": {"PanicException": 1},
        },
        "messages": {
            "m.Shelf.left": {"PanicException": "claim taken"},
            "m.Shelf.right": {"PanicException": "claim taken"},
        },
        "panic_threads": panic_threads,
        **extra,
    }


_TWO_SITES_LOG = (
    "thread '<unnamed>' (101) panicked at src/lib.rs:16:39:\n"
    "claim taken\n"
    "note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace\n"
    "\n"
    "thread '<unnamed>' (102) panicked at src/lib.rs:22:39:\n"
    "claim taken\n"
    "\n"
    "thread '<unnamed>' (101) panicked at src/lib.rs:16:39:\n"
    "claim taken\n"
)


def test_two_sites_sharing_one_message_are_two_findings_joined_by_thread():
    """Matched by message alone, both callables were filed at whichever site
    panicked first, and the second site never appeared."""
    from ftcheck.stress import panic_findings, panic_locations

    result = _two_site_result(
        {"101": [["m.Shelf.left", "claim taken", 2]], "102": [["m.Shelf.right", "claim taken", 1]]}
    )
    findings = panic_findings(result, threads=8, seed=1, locations=panic_locations(_TWO_SITES_LOG))
    by_line = {f["primary"]["line"]: f for f in findings}
    assert sorted(by_line) == [16, 22]
    assert by_line[16]["symbol"] == "m.Shelf.left"
    assert "2 of 40 calls to `m.Shelf.left`" in by_line[16]["message"]
    assert "m.Shelf.right" not in by_line[16]["message"]
    assert "1 of 60 calls to `m.Shelf.right`" in by_line[22]["message"]


def test_a_callable_that_panicked_at_two_sites_is_counted_at_each():
    """One thread that ran both callables (the mix phase): its panics are
    joined in order, so each site gets exactly the calls that panicked there."""
    from ftcheck.stress import panic_findings, panic_locations

    log = (
        "thread '<unnamed>' (103) panicked at src/lib.rs:16:39:\nclaim taken\n"
        "thread '<unnamed>' (103) panicked at src/lib.rs:22:39:\nclaim taken\n"
        "thread '<unnamed>' (103) panicked at src/lib.rs:22:39:\nclaim taken\n"
    )
    result = _two_site_result(
        {"103": [["m.Shelf.left", "claim taken", 1], ["m.Shelf.left", "claim taken", 1]]},
    )
    result["exceptions"] = {"m.Shelf.left": {"PanicException": 2}}
    result["panic_threads"]["103"].insert(1, ["m.Shelf.right", "claim taken", 1])
    result["exceptions"]["m.Shelf.right"] = {"PanicException": 1}
    findings = panic_findings(result, threads=8, seed=1, locations=panic_locations(log))
    by_line = {f["primary"]["line"]: f for f in findings}
    assert sorted(by_line) == [16, 22]
    assert "1 of 40 calls to `m.Shelf.left`" in by_line[16]["message"]
    assert "`m.Shelf.right`" in by_line[22]["message"]
    assert "`m.Shelf.left`" in by_line[22]["message"]


def test_without_thread_ids_every_site_with_the_message_is_listed():
    """Older Rust prints no thread id. The site cannot be told apart then, so
    the finding names every candidate instead of picking one silently."""
    from ftcheck.stress import panic_findings, panic_locations

    log = _TWO_SITES_LOG.replace(" (101)", "").replace(" (102)", "")
    result = _two_site_result({})
    del result["panic_threads"]
    (finding,) = panic_findings(result, threads=8, seed=1, locations=panic_locations(log))
    assert "src/lib.rs:16" in finding["message"] and "src/lib.rs:22" in finding["message"]
    assert "3 of 100 calls" in finding["message"]


def test_declared_test_dependencies_are_found_in_the_usual_places(tmp_path):
    """A first user's two projects both failed test collection: the venv held
    only the wheel and the runner."""
    from ftcheck.ci.pipeline import _declared_test_dependencies as found

    assert found(tmp_path) is None

    (tmp_path / "requirements-dev.txt").write_text("pytest-timeout\n")
    assert found(tmp_path) == ("requirements-dev.txt", ["-r", "requirements-dev.txt"])

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\ntests = ["hypothesis"]\n'
    )
    label, args = found(tmp_path)
    assert label == "extra 'tests'"
    assert args == ["{wheel}", "tests"], "an extra goes on the instrumented wheel, never `.`"

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[dependency-groups]\ntest = ["hypothesis"]\ndev = ["ruff"]\n'
    )
    assert found(tmp_path) == ("dependency group 'test'", ["--group", "test"])


def test_a_setuptools_rust_project_is_named_not_built_without_bindings(tmp_path):
    from ftcheck.ci.pipeline import _build_backend

    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nbuild-backend = "setuptools.build_meta"\n'
    )
    assert "setuptools-rust" in _build_backend(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nbuild-backend = "setuptools.build_meta"\n[tool.maturin]\n'
    )
    assert _build_backend(tmp_path) is None


def test_an_aborted_pytest_session_is_detected(tmp_path):
    """Regression: an INTERNALERROR part-way through the session read as a
    suite that ran and failed."""
    from ftcheck.ci.pipeline import Outcome, _read_session

    log = tmp_path / "pytest.log"
    log.write_text(
        "Collected 40 items to run in parallel\n"
        "INTERNALERROR> RecursionError: maximum recursion depth exceeded\n"
        "==================== 1 passed, 1 error in 3.21s ====================\n"
    )
    out = Outcome(tests_collected=2)
    _read_session(out, log)
    assert out.pytest_summary == "1 passed, 1 error in 3.21s"
    assert "INTERNALERROR" in out.aborted and "of 40 collected" in out.aborted


def test_an_abort_is_a_finding_not_nothing():
    """Regression: `Fatal Python error` produced no finding; with races
    suppressed the run read "nothing was driven"."""
    from ftcheck.stress import _abort_finding

    log = "Fatal Python error: _Py_Dealloc: object freed twice\n"
    f = _abort_finding(-6, log, "pair a x b | calls 551", 8, 3)
    assert f["rule"] == "stress/crash" and "_Py_Dealloc" in f["message"]
    assert _abort_finding(-11, "", "pair a x b", 8, 3)["message"].count("signal 11") == 1
    assert _abort_finding(0, "all fine\n", "done", 8, 3) is None


def test_a_workspace_member_resolves_to_the_workspace_root(tmp_path):
    from ftcheck.ci.pipeline import _source_root

    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    member = tmp_path / "crates" / "py"
    member.mkdir(parents=True)
    (member / "Cargo.toml").write_text('[package]\nname = "py"\n')
    assert _source_root(member) == tmp_path.resolve()

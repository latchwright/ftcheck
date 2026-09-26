# SPDX-License-Identifier: MIT OR Apache-2.0
"""The ci pipeline's inputs: which tests run, what is installed, what is kept."""
import pathlib
import shutil
import subprocess

import pytest
from ftcheck.ci import pipeline
from ftcheck.ci.pipeline import Options, _declared_test_dependencies, _select_tests


def opts(project: pathlib.Path, **kw) -> Options:
    return Options(project=project, work_dir=project / "work", **kw)


def test_default_tests_come_from_testpaths(tmp_path):
    """Regression: `testpaths` was ignored, so a suite outside `tests/` never ran."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "python" / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["python/pkg/tests", "missing"]\n'
    )
    paths, source = _select_tests(opts(tmp_path))
    assert paths == ["python/pkg/tests"], "a testpaths entry that does not exist is dropped"
    assert source == "testpaths in pyproject.toml"


def test_testpaths_are_read_from_ini_files_and_globs(tmp_path):
    (tmp_path / "suite_a").mkdir()
    (tmp_path / "suite_b").mkdir()
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths =\n    suite_*\n")
    assert _select_tests(opts(tmp_path))[0] == ["suite_a", "suite_b"]
    (tmp_path / "pytest.ini").unlink()
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\ntestpaths = suite_b\n")
    assert _select_tests(opts(tmp_path))[0] == ["suite_b"]


def test_without_testpaths_tests_dir_then_the_root(tmp_path):
    assert _select_tests(opts(tmp_path)) == (["."], "no tests/ directory")
    (tmp_path / "tests").mkdir()
    assert _select_tests(opts(tmp_path)) == (["tests"], "tests/ directory")


def test_explicit_tests_win(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["a"]\n')
    (tmp_path / "a").mkdir()
    paths, source = _select_tests(opts(tmp_path, tests=["other"]))
    assert paths == ["other"] and source == "--tests"


def test_requirements_come_from_beside_the_selected_tests(tmp_path):
    """Regression: `tests/requirements.txt` was installed even with `--tests other/`."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "requirements.txt").write_text("hypothesis\n")
    (tmp_path / "other").mkdir()
    assert _declared_test_dependencies(tmp_path, ["other"]) is None
    (tmp_path / "other" / "requirements.txt").write_text("pytest-timeout\n")
    assert _declared_test_dependencies(tmp_path, ["other/test_x.py"]) == (
        "other/requirements.txt",
        ["-r", "other/requirements.txt"],
    )
    assert _declared_test_dependencies(tmp_path, ["tests"]) == (
        "tests/requirements.txt",
        ["-r", "tests/requirements.txt"],
    )



class Recorder:
    """Stands in for `_stream`: records each command and its environment."""

    def __init__(self, fail_on: str = "maturin"):
        self.calls: list[tuple[list[str], dict]] = []
        self.fail_on = fail_on

    def __call__(self, cmd, log_path, **kwargs):
        self.calls.append((cmd, kwargs.get("env") or {}))
        return 1 if self.fail_on in cmd[0] else 0


def environment():
    from ftcheck.ci.environment import Environment

    return Environment(python="python3", toolchain="nightly", interpreter={"executable": "python3"})


def test_the_build_overrides_a_project_strip_setting(tmp_path, monkeypatch):
    """`strip = true` under [tool.maturin] erased every frame name; maturin
    >= 1.12 lets MATURIN_STRIP override pyproject.toml."""
    recorder = Recorder()
    monkeypatch.setattr(pipeline, "_stream", recorder)
    out = pipeline.Outcome()
    assert pipeline.prepare(environment(), opts(tmp_path), out) is None
    (build_cmd, build_env), = recorder.calls
    assert build_cmd[:2] == ["maturin", "build"]
    assert build_env["MATURIN_STRIP"] == "false"
    assert build_env["CARGO_PROFILE_RELEASE_STRIP"] == "false"


@pytest.mark.skipif(not (shutil.which("readelf") and shutil.which("objcopy")), reason="binutils")
def test_a_stripped_library_is_recognised(tmp_path):
    import ftcheck._ftcheck as ext

    unstripped = tmp_path / "unstripped.so"
    shutil.copy(ext.__file__, unstripped)
    if pipeline._stripped(unstripped):
        pytest.skip("the installed extension is itself stripped")
    stripped = tmp_path / "stripped.so"
    subprocess.run(["objcopy", "--strip-all", str(unstripped), str(stripped)], check=True)
    assert pipeline._stripped(stripped)
    not_elf = tmp_path / "not-elf.so"
    not_elf.write_text("not a shared library\n")
    assert not pipeline._stripped(not_elf), "unreadable is not the same as stripped"


def test_build_requirements_are_installed_for_build_scripts(tmp_path, monkeypatch):
    """Regression: a build script needing Python packages (cffi, setuptools)
    failed, because [build-system].requires was never installed."""
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["maturin>=1.5,<2", "cffi>=1.0", "setuptools"]\n'
        'build-backend = "maturin"\n'
    )
    recorder = Recorder()
    monkeypatch.setattr(pipeline, "_stream", recorder)
    assert pipeline.prepare(environment(), opts(tmp_path), pipeline.Outcome()) is None
    (venv_cmd, _), (pip_cmd, _), (build_cmd, build_env) = recorder.calls
    build_venv = tmp_path / "work" / "build-venv"
    assert venv_cmd == ["python3", "-m", "venv", str(build_venv)]
    assert pip_cmd[0] == str(build_venv / "bin" / "python")
    assert pip_cmd[-2:] == ["cffi>=1.0", "setuptools"], "maturin is the image's, not pip's"
    assert build_cmd[0] == "maturin"
    assert build_env["PATH"].split(":")[0] == str(build_venv / "bin")


def test_no_build_requirements_beyond_maturin_means_no_build_venv(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[build-system]\nrequires = ["maturin"]\n')
    recorder = Recorder()
    monkeypatch.setattr(pipeline, "_stream", recorder)
    pipeline.prepare(environment(), opts(tmp_path), pipeline.Outcome())
    assert [cmd[0] for cmd, _ in recorder.calls] == ["maturin"]


def test_a_failed_build_requirement_install_names_its_stage(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[build-system]\nrequires = ["cffi"]\n')
    monkeypatch.setattr(pipeline, "_stream", Recorder(fail_on="build-venv"))
    out = pipeline.Outcome()
    assert pipeline.prepare(environment(), opts(tmp_path), out) is None
    assert out.stage == "build" and "[build-system].requires" in out.error


def test_a_module_that_re_enables_the_gil_is_noted(monkeypatch):
    """Stock free-threaded CPython re-enables the GIL for a module that does not
    declare `gil_used = false`; ftcheck forces it off, and must say so."""
    monkeypatch.setattr(pipeline, "_reenables_gil", lambda python, m, env, cwd: m == "pkg._a")
    (note,) = pipeline._gil_notes("python", ["pkg._a", "pkg._b"], {}, ".")
    assert "`pkg._a`" in note and "gil_used = false" in note and "PYTHON_GIL=0" in note
    monkeypatch.setattr(pipeline, "_reenables_gil", lambda *a: None)
    assert pipeline._gil_notes("python", ["pkg._a"], {}, ".") == [], "inconclusive is silent"


def test_the_gil_probe_runs_without_the_forced_setting(tmp_path):
    import os
    import sys

    (tmp_path / "gil_probe_target.py").write_text(
        "import os\nassert 'PYTHON_GIL' not in os.environ, 'the probe must not force it'\n"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PYTHON_GIL": "0"}
    assert pipeline._reenables_gil(sys.executable, "gil_probe_target", env, tmp_path) is False
    assert pipeline._reenables_gil(sys.executable, "no_such_module_xyz", env, tmp_path) is None


def test_stress_prints_the_pipeline_notes():
    import io
    from types import SimpleNamespace

    from ftcheck.report import text
    from ftcheck.stress import StressOutcome

    env = environment()
    out = StressOutcome(notes=["`pkg._a` does not declare `gil_used = false`"])
    sopts = SimpleNamespace(seed=1, iterations=10, budget_seconds=5)
    stream = io.StringIO()
    text.render_stress(env, out, 0, "headline", sopts, 8, stream)
    assert "`pkg._a` does not declare" in stream.getvalue()


def test_each_run_keeps_its_own_tsan_logs(tmp_path):
    """Regression: one generation of raw logs was kept, so back-to-back seeds
    overwrote the logs of the run before last."""
    made = [pipeline._new_tsan_dir(tmp_path, f"seed{n}") for n in range(7)]
    assert len(set(made)) == 7, "runs in the same second still get their own directory"
    kept = sorted(p.name for p in (tmp_path / "tsan-runs").iterdir())
    assert len(kept) == pipeline._KEEP_TSAN_RUNS == 5
    assert made[-1].is_dir() and made[-1].name.endswith("seed6")
    assert not made[0].exists()
    assert made[-1].name[:8].isdigit() and "Z" in made[-1].name, "named for the UTC time"
    same = [pipeline._new_tsan_dir(tmp_path / "ci", "") for _ in range(2)]
    assert same[0] != same[1] and all(p.is_dir() for p in same)


def session(tmp_path, log: str, **kw) -> pipeline.Outcome:
    path = tmp_path / "pytest.log"
    path.write_text(log)
    out = pipeline.Outcome(**kw)
    pipeline._read_session(out, path)
    return out


def test_a_session_that_ends_early_without_an_internalerror_is_detected(tmp_path):
    """A session that stops part-way without an INTERNALERROR read as a
    suite that ran 2 tests and failed one."""
    out = session(
        tmp_path,
        "Collected 40 items to run in parallel\n"
        "==================== 1 passed, 1 failed in 3.21s ====================\n",
        tests_collected=2,
        pytest_exit=1,
    )
    assert out.aborted == "pytest stopped after 2 of 40 collected tests"

    from ftcheck.ci import verdict
    from ftcheck.exit_codes import UNAVAILABLE

    out.stage = "done"
    assert verdict(environment(), out).code == UNAVAILABLE, "exit 3, not a suite failure"


def test_a_complete_session_is_not_called_early(tmp_path):
    # Skipped and thread-unsafe tests are in the JUnit count, not in the
    # "to run in parallel" count.
    assert session(
        tmp_path,
        "Collected 6 items to run in parallel\n"
        "============ 5 passed, 1 skipped, 1 failed in 0.10s ============\n",
        tests_collected=6, tests_skipped=1, pytest_exit=1,
    ).aborted is None  # fmt: skip
    # pytest's own count comes before deselection.
    assert session(
        tmp_path,
        "collected 7 items / 1 deselected / 6 selected\n"
        "====== 4 passed, 1 skipped, 1 deselected, 1 failed in 0.12s ======\n",
        tests_collected=5, tests_skipped=1, pytest_exit=1,
    ).aborted is None  # fmt: skip
    # -x / --maxfail stop on purpose: a failing suite, not an abort.
    assert session(
        tmp_path,
        "Collected 6 items to run in parallel\n"
        "!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!\n"
        "==================== 1 passed, 1 failed in 0.07s ====================\n",
        tests_collected=2, pytest_exit=1,
    ).aborted is None  # fmt: skip


def test_the_junit_count_keeps_skipped_tests(tmp_path):
    junit = tmp_path / "j.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="2" tests="7">'
        "</testsuite></testsuites>"
    )
    assert pipeline._count_tests(junit) == (5, 1, 0, 2)


def test_a_previous_runs_junit_is_never_read_as_this_ones(tmp_path, monkeypatch):
    """A session killed before pytest wrote its XML must not inherit the counts
    of the run before it."""
    from ftcheck.ci import verdict
    from ftcheck.ci.environment import Check
    from ftcheck.exit_codes import UNAVAILABLE

    o = opts(tmp_path)
    o.work_dir.mkdir()
    (o.work_dir / "pytest-junit.xml").write_text(
        '<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3"/>'
    )
    prepared = pipeline.Prepared(python="python", wheel=tmp_path / "x.whl", tsan_dir=tmp_path)
    monkeypatch.setattr(pipeline, "prepare", lambda *a, **k: prepared)

    def died_quietly(cmd, log_path, **kwargs):
        log_path.write_text("Collected 3 items to run in parallel\n")
        return 0

    monkeypatch.setattr(pipeline, "_stream", died_quietly)
    env = environment()
    env.checks.append(Check("x", True, "ok"))
    out = pipeline.run(env, o)
    assert out.tests_collected == 0
    assert verdict(env, out).code == UNAVAILABLE

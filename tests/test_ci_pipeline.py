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

# SPDX-License-Identifier: MIT OR Apache-2.0
"""The ci pipeline's inputs: which tests run, what is installed, what is kept."""
import pathlib

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


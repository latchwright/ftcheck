# SPDX-License-Identifier: MIT OR Apache-2.0
"""`ftcheck matrix`: free-threaded wheel jobs for a release workflow."""
import pytest
from ftcheck.cli import main
from ftcheck.exit_codes import CLEAN, USAGE
from ftcheck.matrix import Matrix, render

yaml = pytest.importorskip("yaml", reason="PyYAML validates the emitted workflow")


def test_default_output_is_valid_yaml_with_both_job_families():
    doc = yaml.safe_load(render(Matrix()))
    assert set(doc["jobs"]) == {"free-threaded-linux", "free-threaded-native"}


def test_linux_jobs_name_every_interpreter_explicitly():
    """Each interpreter is named, so the build does not depend on discovery."""
    doc = yaml.safe_load(render(Matrix(pythons=["3.14t", "3.15t"])))
    step = doc["jobs"]["free-threaded-linux"]["steps"][1]
    assert step["uses"].startswith("PyO3/maturin-action@")
    assert "-i python3.14t python3.15t" in step["with"]["args"]


def test_native_jobs_hand_maturin_the_exact_interpreter_path():
    doc = yaml.safe_load(render(Matrix()))
    steps = doc["jobs"]["free-threaded-native"]["steps"]
    assert steps[1]["with"]["python-version"] == "${{ matrix.python }}"
    assert "steps.python.outputs.python-path" in steps[2]["with"]["args"]


def test_the_stopgap_is_stated_in_the_output():
    out = render(Matrix())
    assert "STOPGAP" in out and "maturin generate-ci" in out


def test_abi3t_is_emitted_disabled_with_the_reason():
    out = render(Matrix(abi3t=True))
    doc = yaml.safe_load(out)
    assert doc["jobs"]["free-threaded-abi3t"]["if"] is False
    assert "DISABLED until you have confirmed" in out


def test_manifest_path_is_passed_through():
    out = render(Matrix(manifest_path="bindings/python/Cargo.toml"))
    assert "--manifest-path bindings/python/Cargo.toml" in out


def test_a_gil_interpreter_is_refused():
    with pytest.raises(ValueError, match="free-threaded"):
        render(Matrix(pythons=["3.14"]))


def test_an_unknown_platform_is_refused():
    with pytest.raises(ValueError, match="unknown platform"):
        render(Matrix(platforms=["amiga"]))


def test_cli_writes_the_workflow_and_reads_maturin_config(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.maturin]\nmanifest-path = "crates/py/Cargo.toml"\n'
    )
    (tmp_path / "crates/py").mkdir(parents=True)
    (tmp_path / "crates/py/Cargo.toml").write_text(
        '[dependencies]\npyo3 = { version = "0.29", features = ["abi3-py39"] }\n'
    )
    assert main(["matrix", str(tmp_path), "--python", "3.14t"]) == CLEAN
    out = capsys.readouterr().out
    assert "--manifest-path crates/py/Cargo.toml" in out
    assert "abi3 feature" in out
    yaml.safe_load(out)


def test_cli_rejects_a_bad_platform():
    assert main(["matrix", ".", "--platform", "amiga"]) == USAGE


def test_the_action_manifest_is_a_valid_composite_action():
    import pathlib

    doc = yaml.safe_load((pathlib.Path(__file__).parents[1] / "action.yml").read_text())
    assert doc["runs"]["using"] == "composite"
    assert set(doc["inputs"]) == {"command", "path", "args", "sarif"}
    run = doc["runs"]["steps"][1]["run"]
    assert "seccomp=unconfined" in run, "TSan cannot start on GitHub's runners without it"
    assert "${{" not in run, "inputs reach the script through env, never interpolated into it"



def test_the_output_is_a_complete_workflow_github_accepts():
    """A bare `jobs:` block redirected into .github/workflows/ does not load."""
    doc = yaml.safe_load(render(Matrix()))
    assert doc["name"] == "free-threaded wheels"
    triggers = doc.get("on", doc.get(True))  # PyYAML reads the key `on` as True
    assert "push" in triggers and "workflow_dispatch" in triggers

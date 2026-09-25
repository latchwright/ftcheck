# SPDX-License-Identifier: MIT OR Apache-2.0
"""The repository is private. These tests assert it cannot be published by accident.

The last test is the one that matters: it removes a guard and asserts the guard
script notices. A guard nobody has seen fail is a guard nobody knows works.
"""
import pathlib
import shutil
import subprocess
import tempfile
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check-private.sh"


def test_every_crate_sets_publish_false():
    manifests = list((ROOT / "crates").glob("*/Cargo.toml"))
    assert manifests, "no crates found"
    for manifest in manifests:
        data = tomllib.loads(manifest.read_text())
        assert data["package"]["publish"] is False, f"{manifest} may be published"


def test_pyproject_carries_private_classifier():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "Private :: Do Not Upload" in data["project"]["classifiers"]


def test_no_release_workflow_exists():
    workflows = ROOT / ".github" / "workflows"
    names = {p.name for p in workflows.glob("*.yml")} if workflows.exists() else set()
    offenders = {n for n in names if "release" in n or "publish" in n}
    assert not offenders, f"release workflow present: {offenders}"


def test_guard_script_passes():
    assert subprocess.run([GUARD], cwd=ROOT, check=False).returncode == 0


def test_guard_script_fails_when_the_classifier_is_removed():
    with tempfile.TemporaryDirectory() as tmp:
        copy = pathlib.Path(tmp) / "repo"
        shutil.copytree(
            ROOT, copy, ignore=shutil.ignore_patterns(".git", "target", ".venv*", ".idea", ".tmp", "dist*")
        )
        pyproject = copy / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text().replace('    "Private :: Do Not Upload",\n', "")
        )
        result = subprocess.run(
            [copy / "scripts" / "check-private.sh"], cwd=copy, capture_output=True, text=True, check=False
        )
        assert result.returncode != 0, "guard did not notice the missing classifier"
        assert "classifier" in result.stderr


def test_guard_script_fails_when_a_crate_becomes_publishable():
    with tempfile.TemporaryDirectory() as tmp:
        copy = pathlib.Path(tmp) / "repo"
        shutil.copytree(
            ROOT, copy, ignore=shutil.ignore_patterns(".git", "target", ".venv*", ".idea", ".tmp", "dist*")
        )
        manifest = next((copy / "crates").glob("*/Cargo.toml"))
        manifest.write_text(manifest.read_text().replace("publish = false", "publish = true"))
        result = subprocess.run(
            [copy / "scripts" / "check-private.sh"], cwd=copy, capture_output=True, text=True, check=False
        )
        assert result.returncode != 0, "guard did not notice a publishable crate"

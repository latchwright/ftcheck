# SPDX-License-Identifier: MIT OR Apache-2.0
"""ftcheck is published one way only. These tests assert the guard enforces it.

The Python package ships from .github/workflows/release.yml, on a `v*` tag, to
PyPI through Trusted Publishing. Most tests below break one guard in a copy of
the tree and assert the guard script notices. A guard nobody has seen fail is a
guard nobody knows works.
"""
import pathlib
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = "scripts/check-publish-guards.sh"
RELEASE = ".github/workflows/release.yml"


def test_every_crate_sets_publish_false():
    manifests = list((ROOT / "crates").glob("*/Cargo.toml"))
    assert manifests, "no crates found"
    for manifest in manifests:
        data = tomllib.loads(manifest.read_text())
        assert data["package"]["publish"] is False, f"{manifest} may be published"


def test_pyproject_does_not_block_the_upload():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert not [c for c in data["project"]["classifiers"] if c.startswith("Private ::")]


def test_guard_script_passes():
    result = subprocess.run([ROOT / GUARD], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def _guard_on_broken_copy(break_it: Callable[[pathlib.Path], None]) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        copy = pathlib.Path(tmp) / "repo"
        shutil.copytree(
            ROOT,
            copy,
            ignore=shutil.ignore_patterns(".git", "target", ".venv*", ".idea", ".tmp", "dist*"),
        )
        break_it(copy)
        return subprocess.run(
            [copy / GUARD], cwd=copy, capture_output=True, text=True, check=False
        )


def _edit(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}; the test no longer breaks anything"
    path.write_text(text.replace(old, new, 1))


def _crate_becomes_publishable(copy: pathlib.Path) -> None:
    _edit(next((copy / "crates").glob("*/Cargo.toml")), "publish = false", "publish = true")


def _token_based_publish(copy: pathlib.Path) -> None:
    _edit(
        copy / RELEASE,
        "          attestations: true\n",
        "          attestations: true\n          password: ${{ secrets.PYPI_API_TOKEN }}\n",
    )


def _password_without_secrets(copy: pathlib.Path) -> None:
    _edit(
        copy / RELEASE,
        "          attestations: true\n",
        "          attestations: true\n          password: ${{ env.PYPI_TOKEN }}\n",
    )


def _publish_to_another_index(copy: pathlib.Path) -> None:
    _edit(
        copy / RELEASE,
        "          attestations: true\n",
        "          attestations: true\n          repository-url: https://example.invalid/legacy/\n",
    )


def _twine_upload(copy: pathlib.Path) -> None:
    _edit(copy / RELEASE, "      - run: ls -l dist\n", "      - run: twine upload dist/*\n")


def _no_oidc_token(copy: pathlib.Path) -> None:
    _edit(copy / RELEASE, "      id-token: write\n", "      contents: read\n")


def _no_environment(copy: pathlib.Path) -> None:
    _edit(copy / RELEASE, "    environment: pypi\n", "")


def _no_pypa_action(copy: pathlib.Path) -> None:
    _edit(copy / RELEASE, "pypa/gh-action-pypi-publish@", "example/publish@")


def _second_publishing_workflow(copy: pathlib.Path) -> None:
    shutil.copy(copy / RELEASE, copy / ".github/workflows/deploy.yml")


def _ci_uploads_too(copy: pathlib.Path) -> None:
    ci = copy / ".github/workflows/ci.yml"
    ci.write_text(ci.read_text() + "      - run: maturin publish\n")


def _runs_on_branch_pushes(copy: pathlib.Path) -> None:
    _edit(copy / RELEASE, "    tags: ['v*']\n", "    tags: ['v*']\n    branches: [main]\n")


def _runs_by_hand(copy: pathlib.Path) -> None:
    _edit(copy / RELEASE, "    tags: ['v*']\n", "    tags: ['v*']\n  workflow_dispatch:\n")


def _release_workflow_removed(copy: pathlib.Path) -> None:
    (copy / RELEASE).unlink()


@pytest.mark.parametrize(
    ("break_it", "complaint"),
    [
        (_crate_becomes_publishable, "publish = false"),
        (_token_based_publish, "secret"),
        (_password_without_secrets, "credential"),
        (_publish_to_another_index, "repository"),
        (_twine_upload, "other than pypa/gh-action-pypi-publish"),
        (_no_oidc_token, "id-token"),
        (_no_environment, "environment"),
        (_no_pypa_action, "does not publish with pypa/gh-action-pypi-publish"),
        (_second_publishing_workflow, "deploy.yml publishes"),
        (_ci_uploads_too, "ci.yml publishes"),
        (_runs_on_branch_pushes, "triggered only by"),
        (_runs_by_hand, "triggered only by"),
        (_release_workflow_removed, "missing"),
    ],
    ids=lambda v: v.__name__.strip("_") if callable(v) else None,
)
def test_guard_script_fails_when_a_guard_is_broken(break_it, complaint):
    result = _guard_on_broken_copy(break_it)
    assert result.returncode != 0, f"guard did not notice: {break_it.__name__}"
    assert complaint in result.stderr, result.stderr

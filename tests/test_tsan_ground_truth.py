# SPDX-License-Identifier: MIT OR Apache-2.0
"""The sanitizer half of the ground truth: every fixture, under `ci` and `stress`.

Opt-in, because it needs Docker and takes minutes:

    docker build -f docker/Dockerfile -t ftcheck-tsan .
    FTCHECK_TSAN=1 pytest tests/test_tsan_ground_truth.py

Without FTCHECK_TSAN the whole module is skipped, visibly. With it, a missing
Docker or image is a failure, not a skip — CI sets it, so the job cannot go
green by quietly running nothing.

All fixtures run in one container sharing one Cargo target directory, so the
instrumented standard library is built once rather than once per fixture.
"""
import json
import os
import pathlib
import shutil
import subprocess

import pytest
from ftcheck.fixtures import discover

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = discover(ROOT / "fixtures")
IMAGE = os.environ.get("FTCHECK_TSAN_IMAGE", "ftcheck-tsan")
OUT = ROOT / "target" / "tsan-ground-truth"

if not os.environ.get("FTCHECK_TSAN"):
    pytest.skip(
        "sanitizer ground truth not requested: set FTCHECK_TSAN=1 "
        "(needs Docker and the ftcheck-tsan image)",
        allow_module_level=True,
    )

_SCRIPT = r"""
set -u
for rel in "$@"; do
  name=$(basename "$rel")
  python3 -m ftcheck ci "/ftcheck/fixtures/$rel" \
      --work-dir "/out/work/$name" --target-dir /out/target \
      --format json > "/out/$name.json" 2> "/out/$name.stderr"
  echo "$rel ci exit=$?"
  python3 -m ftcheck stress "/ftcheck/fixtures/$rel" \
      --work-dir "/out/work/$name" --target-dir /out/target \
      --seed 20260924 --budget 60 \
      --format json > "/out/$name.stress.json" 2> "/out/$name.stress.stderr"
  echo "$rel stress exit=$?"
done
"""


@pytest.fixture(scope="module")
def results():
    if not shutil.which("docker"):
        pytest.fail("FTCHECK_TSAN is set but docker is not on PATH")
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, check=False
    )
    if inspect.returncode != 0:
        pytest.fail(f"FTCHECK_TSAN is set but image {IMAGE!r} is missing: "
                    "docker build -f docker/Dockerfile -t ftcheck-tsan .")

    (OUT / "cargo-registry").mkdir(parents=True, exist_ok=True)
    runnable = [
        str(p.parent.relative_to(ROOT / "fixtures")) for p, e in FIXTURES if not e.tsan_pending
    ]
    for rel in runnable:
        for suffix in (".json", ".stress.json"):
            (OUT / f"{pathlib.Path(rel).name}{suffix}").unlink(missing_ok=True)

    cmd = [
        "docker", "run", "--rm",
        # Lets TSan disable ASLR for its own process, so the host's
        # vm.mmap_rnd_bits does not matter.
        "--security-opt", "seccomp=unconfined",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/tmp",
        "-e", "PYTHONPATH=/ftcheck/python",
        "-v", f"{ROOT}:/ftcheck:ro",
        "-v", f"{OUT}:/out",
        # The image carries no crate registry; a persistent one saves re-fetching
        # the index on every run.
        "-v", f"{OUT / 'cargo-registry'}:/opt/cargo/registry",
        IMAGE, "bash", "-c", _SCRIPT, "ftcheck-ground-truth", *runnable,
    ]  # fmt: skip
    log = OUT / "run.log"
    with open(log, "w") as fh:
        subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=3600, check=False)

    found = {}
    for rel in runnable:
        name = pathlib.Path(rel).name
        for mode, suffix in (("ci", ""), ("stress", ".stress")):
            path = OUT / f"{name}{suffix}.json"
            try:
                found[(name, mode)] = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                found[(name, mode)] = {
                    "harness_error": f"no report ({exc}); see {OUT / (name + suffix + '.stderr')}"
                }
    return found


def _text(finding):
    frames = [f["symbol"] for s in finding["stacks"] for f in s["frames"]]
    return " ".join([finding["message"], finding["symbol"], *frames])


def _check(report, name, must_race, mode):
    assert "harness_error" not in report, report.get("harness_error")
    assert not report["fatal"], report["fatal"]
    if must_race:
        assert report["exit_code"] == 1, f"{name} [{mode}]: expected a race, got {report['verdict']!r}"
        assert report["findings"], f"{name} [{mode}]: TSan must report a race in the extension"
        assert all(f["rule"].startswith("tsan/") for f in report["findings"])
    else:
        assert report["findings"] == [], (
            f"{name} [{mode}]: false positive — {[f['primary'] for f in report['findings']]}. "
            "A finding on a clean fixture fails as hard as a miss on a racy one."
        )
        assert report["exit_code"] == 0, f"{name} [{mode}]: {report['verdict']!r}"


def _symbols_present(report, symbols):
    text = " ".join(_text(f) for f in report["findings"])
    return any(s in text for s in symbols)


IDS = [p.parent.name for p, _ in FIXTURES]


@pytest.mark.parametrize("path,expectation", FIXTURES, ids=IDS)
def test_ci_matches_ground_truth(results, path, expectation):
    if expectation.tsan_pending:
        pytest.xfail(f"sanitizer half pending: {expectation.tsan_pending}")
    name = path.parent.name
    report = results[(name, "ci")]
    _check(report, name, expectation.ci_must_race, "ci")
    assert report["tests_collected"] >= 1, f"{name}: nothing was exercised"
    if expectation.ci_must_race and expectation.symbols:
        assert _symbols_present(report, expectation.symbols), (
            f"{name}: none of {expectation.symbols} appears in the reported stacks"
        )


@pytest.mark.parametrize("path,expectation", FIXTURES, ids=IDS)
def test_stress_matches_ground_truth(results, path, expectation):
    """Stress must see every race, including those the fixture's tests cannot."""
    if expectation.tsan_pending:
        pytest.xfail(f"sanitizer half pending: {expectation.tsan_pending}")
    name = path.parent.name
    report = results[(name, "stress")]
    if expectation.stress_rules:
        assert "harness_error" not in report, report.get("harness_error")
        reported = sorted({f["rule"] for f in report["findings"] if not f["rule"].startswith("tsan/")})
        assert reported == sorted(expectation.stress_rules), f"{name}: got {reported}"
        tsan = [f for f in report["findings"] if f["rule"].startswith("tsan/")]
        assert bool(tsan) == expectation.race, f"{name}: TSan findings {tsan}"
        assert report["exit_code"] == 1, f"{name}: {report['verdict']!r}"
        return
    _check(report, name, expectation.race, "stress")
    groups = (report.get("stress") or {}).get("groups", [])
    confined = sum(len(g.get("confined_callables", [])) for g in groups)
    assert report["coverage"]["driven"] >= 1 or confined >= 1, (
        f"{name}: stress drove nothing, and nothing was thread-confined"
    )
    assert report["seed"] == 20260924
    if expectation.race and expectation.symbols:
        assert _symbols_present(report, expectation.symbols), (
            f"{name}: none of {expectation.symbols} appears in the reported stacks"
        )


def test_the_gap_fixture_is_invisible_to_ci_and_caught_by_stress(results):
    """The single claim `stress` exists to prove, asserted directly."""
    ci = results[("stress-unshared-state", "ci")]
    stress = results[("stress-unshared-state", "stress")]
    assert ci["exit_code"] == 0 and ci["findings"] == []
    assert stress["exit_code"] == 1 and stress["findings"]


def test_two_panic_sites_with_one_message_are_reported_at_their_own_lines(results):
    """Matched by message, both methods were filed under one line."""
    report = results[("stress-panic-two-sites", "stress")]
    panics = [f for f in report["findings"] if f["rule"] == "stress/panic"]
    by_line = {f["primary"]["line"]: f for f in panics}
    assert sorted(by_line) == [28, 34], [f["primary"] for f in panics]
    assert by_line[28]["symbol"].endswith("Queue.head")
    assert "Queue.tail" not in by_line[28]["message"]
    assert by_line[34]["symbol"].endswith("Queue.tail")
    assert "Queue.head" not in by_line[34]["message"]

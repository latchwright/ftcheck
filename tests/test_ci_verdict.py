# SPDX-License-Identifier: MIT OR Apache-2.0
"""The ci verdict: "clean" is what remains when every other explanation is ruled out."""
from ftcheck.ci import verdict
from ftcheck.ci.environment import Check, Environment
from ftcheck.ci.pipeline import Outcome
from ftcheck.exit_codes import CLEAN, FINDINGS, UNAVAILABLE


def ready():
    return Environment(python="python3", toolchain="nightly", checks=[Check("x", True, "ok")])


def ran(**kw):
    base = {"stage": "done", "pytest_exit": 0, "tests_collected": 3}
    base.update(kw)
    return Outcome(**base)


FINDING = {"rule": "tsan/data-race", "primary": {"file": "src/lib.rs", "line": 1, "column": 1}}


def test_a_failed_preflight_could_not_run():
    env = Environment(python="p", toolchain="t", checks=[Check("llvm_match", False, "21 vs 22")])
    v = verdict(env, None)
    assert v.code == UNAVAILABLE
    assert "llvm_match" in v.headline


def test_a_preflight_warning_does_not_block():
    env = ready()
    env.checks.append(Check("python_version", False, "3.13t", warning=True))
    assert verdict(env, ran()).code == CLEAN


def test_a_failed_build_could_not_run():
    v = verdict(ready(), Outcome(stage="build", error="the instrumented build failed"))
    assert v.code == UNAVAILABLE
    assert "build" in v.headline


def test_a_tsan_abort_before_any_finding_could_not_run():
    v = verdict(ready(), ran(fatal=["FATAL: ThreadSanitizer: ..."]))
    assert v.code == UNAVAILABLE


def test_confirmed_findings_outrank_a_later_abort():
    """Confirmed findings, then a TSan abort on the heap the race had
    corrupted. The abort must not turn them into exit 3."""
    v = verdict(ready(), ran(fatal=["ThreadSanitizer: CHECK failed"], findings=[FINDING]))
    assert v.code == FINDINGS


def test_a_race_in_the_extension_is_a_finding():
    assert verdict(ready(), ran(findings=[FINDING])).code == FINDINGS


def test_a_race_outside_the_extension_does_not_fail_the_run():
    assert verdict(ready(), ran(external=[FINDING])).code == CLEAN


def test_no_tests_collected_is_not_clean():
    assert verdict(ready(), ran(pytest_exit=5, tests_collected=0)).code == UNAVAILABLE


def test_zero_executed_tests_is_not_clean_even_when_pytest_says_ok():
    """Everything skipped: pytest exits 0, but nothing was exercised."""
    assert verdict(ready(), ran(pytest_exit=0, tests_collected=0)).code == UNAVAILABLE


def test_a_failing_suite_fails_without_claiming_a_race():
    """Its own exit code: a first user could not tell it from a race (both were 1)."""
    from ftcheck.exit_codes import SUITE_FAILED

    v = verdict(ready(), ran(pytest_exit=1, tests_failed=2))
    assert v.code == SUITE_FAILED
    assert "no race" in v.headline


def test_clean_never_says_safe():
    v = verdict(ready(), ran())
    assert v.code == CLEAN
    assert "safe" not in v.headline.lower()
    assert "exercised surface" in v.headline


def test_the_headline_names_each_kind_of_finding():
    from ftcheck.ci import describe_findings

    tsan = {"rule": "tsan/data-race"}
    panic = {"rule": "stress/panic"}
    assert describe_findings([tsan]) == "1 ThreadSanitizer report in your extension"
    assert describe_findings([panic]) == "1 panic under concurrency in your extension"
    assert describe_findings([tsan, tsan, panic]) == (
        "2 ThreadSanitizer reports and 1 panic under concurrency in your extension"
    )


def test_a_panic_inside_a_dependency_is_not_called_yours():
    from ftcheck.ci import describe_findings

    tsan = {"rule": "tsan/data-race"}
    dep = {"rule": "stress/panic", "dependency": "tinyqueue 1.2.3"}
    assert describe_findings([dep, dep]) == (
        "2 panics under concurrency inside a dependency, reached from your extension"
    )
    assert describe_findings([tsan, dep]) == (
        "1 ThreadSanitizer report in your extension and 1 panic under concurrency "
        "inside a dependency, reached from your extension"
    )

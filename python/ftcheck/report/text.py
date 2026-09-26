# SPDX-License-Identifier: MIT OR Apache-2.0
"""Terminal summary.

Never says "safe". The strongest claim available is that nothing was found on
the surface that was actually exercised, and naming what was *not* examined is
part of the report rather than a footnote.
"""
from __future__ import annotations

import shutil
import textwrap
from typing import Any, TextIO

_PYTEST_DID_NOT_RUN = {
    2: "interrupted",
    3: "internal error",
    4: "usage error",
    5: "no tests collected",
}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _wrap(body: str, indent: str) -> str:
    """Wrap prose to the terminal, so a justification stays readable."""
    width = max(50, min(shutil.get_terminal_size((100, 24)).columns, 100) - len(indent))
    return textwrap.fill(
        body, width=width, initial_indent=indent, subsequent_indent=indent
    )


def render(report: dict[str, Any], stream: TextIO, min_confidence: str = "certain") -> None:
    findings = report.get("findings", [])
    unparseable = report.get("unparseable", [])

    coverage = (
        f"scanned {_plural(report.get('files_scanned', 0), 'file')}, "
        f"{_plural(report.get('entry_points_seen', 0), 'Python entry point')}"
    )
    version = report.get("pyo3_version")
    coverage += f", PyO3 {version}" if version else ", PyO3 version unresolved"
    print(coverage, file=stream)

    if unparseable:
        print(f"\ncould not parse {_plural(len(unparseable), 'file')}:", file=stream)
        for path, error in unparseable:
            first_line = str(error).splitlines()[0] if error else "unknown error"
            print(f"  {path}: {first_line}", file=stream)
        print("  (these were not examined — findings in them would not appear)", file=stream)

    if not findings:
        print("\nno findings on the exercised surface", file=stream)
        if min_confidence == "certain":
            print(
                "  `likely` findings were not shown; re-run with --confidence likely",
                file=stream,
            )
        return

    print(f"\n{_plural(len(findings), 'finding')}:\n", file=stream)
    for f in findings:
        loc = f["primary"]
        print(
            f"  {f['rule']}  {loc['file']}:{loc['line']}:{loc['column']}"
            f"  [{f['confidence']}]",
            file=stream,
        )
        print(_wrap(f["message"], "        "), file=stream)
        if f.get("justification"):
            print(_wrap(f["justification"], "        "), file=stream)
        print(file=stream)

    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    print("  " + ", ".join(f"{rule}: {n}" for rule, n in sorted(by_rule.items())), file=stream)


def _location(loc: dict | None) -> str:
    return f"{loc['file']}:{loc['line']}:{loc['column']}" if loc else "<no source location>"


def ci_json(env, outcome, code: int, headline: str, threads: int) -> dict[str, Any]:
    """Everything `render_ci` shows, as data."""
    return {
        "exit_code": code,
        "verdict": headline,
        "environment": {
            "python": env.interpreter.get("executable", env.python),
            "python_version": env.interpreter.get("version_string"),
            "toolchain": env.toolchain,
            "llvm": env.llvm_major,
            "checks": [
                {"name": c.name, "ok": c.ok, "warning": c.warning, "detail": c.detail}
                for c in env.checks
            ],
        },
        "stage": outcome.stage if outcome else None,
        "error": outcome.error if outcome else None,
        "extension_modules": outcome.extension_modules if outcome else [],
        "threads": threads,
        "tests_collected": outcome.tests_collected if outcome else 0,
        "tests_failed": outcome.tests_failed if outcome else 0,
        "tests_errored": outcome.tests_errored if outcome else 0,
        "pytest_exit": outcome.pytest_exit if outcome else None,
        "findings": outcome.findings if outcome else [],
        "reached": outcome.reached if outcome else [],
        "warnings": outcome.warnings if outcome else [],
        "notes": outcome.notes if outcome else [],
        "log_path": outcome.log_path if outcome else None,
        "external": outcome.external if outcome else [],
        "fatal": outcome.fatal if outcome else [],
    }


def _thread(i: int, stack: dict) -> str:
    """`thread 1 (ftm-refill)`: which thread made the access, when known."""
    name = stack.get("thread")
    return f"thread {i} ({name})" if name else f"thread {i}"


def _render_findings(outcome, stream: TextIO) -> None:
    """Warnings, findings in the user's extension, then reports outside it."""
    for warning in outcome.warnings:
        print(file=stream)
        print(_wrap("warning: " + warning, "  "), file=stream)
    if outcome.findings:
        print(f"\n{_plural(len(outcome.findings), 'finding')}:\n", file=stream)
        for f in outcome.findings:
            seen = f.get("occurrences", 1)
            # A stress finding has no source line; the callable is its location.
            located = not f["primary"]["file"].startswith("<")
            where = (
                _location(f["primary"])
                if located or f.get("producer") != "stress"
                else f"`{f['symbol']}`"
            )
            print(
                f"  {f['rule']}  {where}  [{f['confidence']}]"
                + (f"  (reported {seen}x)" if seen > 1 else ""),
                file=stream,
            )
            print(_wrap(f["message"], "        "), file=stream)
            for i, stack in enumerate(f["stacks"], 1):
                frames = stack["frames"][:4]
                print(f"        {_thread(i, stack)}:", file=stream)
                for fr in frames:
                    print(f"          {fr['symbol']}  {_location(fr['location'])}", file=stream)
            print(file=stream)

    if outcome.reached:
        print(
            f"\n{_plural(len(outcome.reached), 'report')} inside CPython or another library, "
            "reached from your extension (likely not your bug; shown, not failing):",
            file=stream,
        )
        for f in outcome.reached:
            print(f"  {f['rule']}  in `{f['symbol']}`  {_location(f['primary'])}", file=stream)
            for i, stack in enumerate(f["stacks"], 1):
                top = stack["frames"][:2]
                path = " <- ".join(fr["symbol"] for fr in top)
                print(f"        {_thread(i, stack)}: {path}", file=stream)

    if outcome.external:
        print(
            f"\n{_plural(len(outcome.external), 'report')} outside your extension "
            "(CPython or another native library; shown, not failing):",
            file=stream,
        )
        for f in outcome.external:
            print(f"  {f['rule']}  in `{f['symbol']}`  {_location(f['primary'])}", file=stream)


def render_ci(env, outcome, code: int, headline: str, threads: int, stream: TextIO) -> None:
    """The `ftcheck ci` summary: environment, what ran, what was found."""
    print("environment", file=stream)
    for c in env.checks:
        mark = "ok  " if c.ok else ("warn" if c.warning else "FAIL")
        print(f"  {mark}  {c.name:<17}{c.detail if c.ok else ''}", file=stream)
        if not c.ok:
            print(_wrap(c.detail, "        "), file=stream)

    if outcome is not None:
        if outcome.extension_modules:
            print(
                f"\nbuilt {', '.join(outcome.extension_modules)} with ThreadSanitizer",
                file=stream,
            )
        for note in outcome.notes:
            print(_wrap(note, ""), file=stream)
        if outcome.pytest_exit in _PYTEST_DID_NOT_RUN:
            # Collection errors show up as tests in pytest's XML; "ran 8 tests"
            # when none ran told a first user nothing.
            reason = _PYTEST_DID_NOT_RUN[outcome.pytest_exit]
            print(
                f"pytest did not run the suite (exit {outcome.pytest_exit}: {reason})",
                file=stream,
            )
        elif outcome.pytest_exit is not None:
            print(
                f"ran {_plural(outcome.tests_collected, 'test')} x {threads} threads "
                f"(pytest exit {outcome.pytest_exit}; "
                f"{outcome.tests_failed} failed, {outcome.tests_errored} errors)",
                file=stream,
            )
        if getattr(outcome, "pytest_summary", None):
            print(f"pytest: {outcome.pytest_summary}", file=stream)
        if getattr(outcome, "aborted", None):
            print(f"{outcome.aborted}", file=stream)
        broken = outcome.error or outcome.pytest_exit not in (0, None)
        if broken and outcome.log:
            what = outcome.error or "pytest"
            print(f"\n{what}; last lines of its log:", file=stream)
            for line in outcome.log.splitlines():
                print(f"  | {line}", file=stream)
            if outcome.log_path:
                print(f"  full log: {outcome.log_path}", file=stream)

        _render_findings(outcome, stream)

        for line in outcome.fatal:
            print(f"\n  {line}", file=stream)

    print(f"\n{headline}", file=stream)


def _render_environment(env, stream: TextIO) -> None:
    failed = [c for c in env.checks if not c.ok]
    if not failed:
        print(f"environment ok ({len(env.checks)} checks)", file=stream)
        return
    print("environment", file=stream)
    for c in env.checks:
        mark = "ok  " if c.ok else ("warn" if c.warning else "FAIL")
        print(f"  {mark}  {c.name:<17}{c.detail if c.ok else ''}", file=stream)
        if not c.ok:
            print(_wrap(c.detail, "        "), file=stream)


def render_stress(
    env, outcome, code: int, headline: str, sopts, threads: int, stream, replay: str = ""
) -> None:
    """The `ftcheck stress` summary: the seed first, because it is what reproduces."""
    from ftcheck.stress import coverage

    _render_environment(env, stream)
    print(
        f"\nseed {sopts.seed}  threads {threads}  iterations {sopts.iterations}  "
        f"budget {sopts.budget_seconds:g}s",
        file=stream,
    )
    print(
        "  replay: " + (replay or f"ftcheck stress --replay {sopts.seed} --threads {threads}"),
        file=stream,
    )

    if outcome is not None:
        if outcome.extension_modules:
            print(f"\nbuilt {', '.join(outcome.extension_modules)} with ThreadSanitizer", file=stream)
        for note in outcome.notes:
            print(_wrap(note, ""), file=stream)
        if outcome.error:
            print(f"\n{outcome.error}", file=stream)
            for line in (outcome.log or "").splitlines():
                print(f"  | {line}", file=stream)

        result = outcome.result
        if result:
            for err in result.get("import_errors", []):
                print(f"\n  could not import {err['module']}: {err['error']}", file=stream)
            driven, total = coverage(result)
            print(f"\ncoverage: {driven} of {total} callables driven", file=stream)
            if result.get("complete") is False:
                print(
                    "  (the run ended early — crash, hang or kill — so this is what was driven "
                    "before it ended)",
                    file=stream,
                )
            for g in result["groups"]:
                pairs = f"{g['pairs_driven']}/{g['pairs_total']} pairs"
                if g.get("in_progress"):
                    pairs += " (in progress when the run ended)"
                if g["budget_exhausted"]:
                    pairs += " (budget exhausted)"
                print(f"\n  {g['name']}  [{g['factory'] or 'no factory'}]  {pairs}", file=stream)
                for q in g["driven"]:
                    excs = result["exceptions"].get(q, {})
                    calls = result["calls"].get(q, 0)
                    # Absent from "returned" means no call returned.
                    returned = result.get("returned")
                    ok = returned.get(q, 0) if returned is not None else calls
                    note = ", ".join(f"{k} x{v}" for k, v in sorted(excs.items()))
                    label = "driven    " if ok else ("not reached" if not calls else "RAISED ALL")
                    print(
                        f"    {label}  {q}  ({calls} calls{'; raised ' + note if note else ''})",
                        file=stream,
                    )
                    if not ok and calls:
                        top = max(excs.items(), key=lambda kv: kv[1])[0] if excs else None
                        why = result.get("messages", {}).get(q, {}).get(top, "") if top else ""
                        print(
                            _wrap(
                                "every call raised"
                                + (f" ({top}: {why})" if why else "")
                                + ": only its argument checks ran. Declare valid arguments "
                                "in [stress.args].",
                                "                ",
                            ),
                            file=stream,
                        )
                if g.get("confined"):
                    print(_wrap(g["confined"], "    confined    "), file=stream)
                for q, why in sorted(g["not_driven"].items()):
                    print(f"    NOT driven  {q}", file=stream)
                    print(_wrap(why, "                "), file=stream)
                for err in g.get("worker_errors", []):
                    print(f"    driver error: {err.strip().splitlines()[-1]}", file=stream)

        _render_findings(outcome, stream)
        for line in outcome.fatal:
            print(f"\n  {line}", file=stream)

    print(f"\n{headline}", file=stream)

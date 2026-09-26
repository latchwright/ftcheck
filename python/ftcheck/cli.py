# SPDX-License-Identifier: MIT OR Apache-2.0
"""The ftcheck command line.

Every command is implemented. A command that cannot run returns UNAVAILABLE
rather than pretending a clean result.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from collections.abc import Sequence

from ftcheck import __version__
from ftcheck import lint as run_lint
from ftcheck.exit_codes import CLEAN, FINDINGS, UNAVAILABLE, USAGE
from ftcheck.report import text

# Phase numbers refer to the product specification's phasing table.
_NOT_IMPLEMENTED: dict[str, tuple[str, str, str]] = {}


class _Parser(argparse.ArgumentParser):
    """Route argparse's own failures through our exit-code contract."""

    def error(self, message: str):  # pragma: no cover - exercised via main()
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(USAGE)


def build_parser() -> _Parser:
    parser = _Parser(prog="ftcheck", description=__doc__)
    parser.add_argument("--version", action="version", version=f"ftcheck {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    lint_parser = subparsers.add_parser(
        "lint",
        help="static free-threading hazard check (a fast pre-flight, not the product)",
    )
    lint_parser.add_argument("path", help="crate root to lint")
    lint_parser.add_argument(
        "--confidence",
        choices=["certain", "likely"],
        default="certain",
        help="minimum confidence to report (default: certain)",
    )
    lint_parser.add_argument("--pyo3-version", help="override the resolved PyO3 version")
    lint_parser.add_argument("--sarif", metavar="FILE", help="write SARIF 2.1.0 here")
    lint_parser.add_argument("--junit", metavar="FILE", help="write JUnit XML here")
    lint_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="stdout format"
    )

    ci_parser = subparsers.add_parser(
        "ci",
        help="build under ThreadSanitizer and run the suite concurrently",
        description=(
            "Run inside a TSan-instrumented free-threaded CPython (the ftcheck-tsan "
            "image, or ghcr.io/nascheme/cpython-tsan with a matching nightly Rust). "
            "Builds the extension with -Zsanitizer=thread and -Zbuild-std, installs it "
            "into an isolated venv, runs the project's tests with every test body in N "
            "threads at once, and reports ThreadSanitizer findings."
        ),
    )
    _add_sanitizer_arguments(ci_parser, "ftcheck-ci")
    ci_parser.add_argument(
        "--tests",
        action="append",
        default=[],
        metavar="PATH",
        help="test paths, relative to the project (default: pytest's testpaths, else tests/ "
        "if present, else the project root)",
    )
    ci_parser.add_argument(
        "--extra", action="append", default=[], help="install the project with this extra"
    )
    ci_parser.add_argument(
        "--pytest-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="pass one argument to pytest, e.g. --pytest-arg=-k --pytest-arg='not network'; "
        "repeatable",
    )
    ci_parser.add_argument(
        "--no-test-deps",
        action="store_true",
        help="do not install the project's declared test dependencies (dependency group, "
        "extra or requirements file named test/tests/testing)",
    )

    stress_parser = subparsers.add_parser(
        "stress",
        help="drive one shared instance of each type from many threads under TSan",
        description=(
            "Builds and installs like `ci`, then drives the native modules' surface: one "
            "instance per type, deliberately shared across N threads, every pair of "
            "methods overlapping, under a recorded seed. Factories come from "
            "[tool.ftcheck.stress] in pyproject.toml or [stress] in ftcheck.toml; "
            "constructors with no or only primitive arguments are derived. Every "
            "callable not driven is named, with the reason."
        ),
    )
    _add_sanitizer_arguments(stress_parser, "ftcheck-stress")
    seed = stress_parser.add_mutually_exclusive_group()
    seed.add_argument("--seed", type=int, help="schedule seed (default: random, printed)")
    seed.add_argument(
        "--replay", type=int, metavar="SEED", help="rerun the schedule a previous run printed"
    )
    stress_parser.add_argument(
        "--budget",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="wall-clock budget for driving (default: 120); pairs not reached are reported",
    )
    stress_parser.add_argument(
        "--iterations",
        type=int,
        default=200,
        help="calls per thread per phase (default: 200)",
    )
    stress_parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="drive only this type or module (dotted name); repeatable",
    )
    stress_parser.add_argument(
        "--min-coverage",
        type=float,
        default=0.0,
        metavar="PERCENT",
        help="exit 3 unless at least this percentage of the surface was driven (default: 0)",
    )
    stress_parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="native module to drive (default: from config, else every one in the wheel)",
    )

    matrix_parser = subparsers.add_parser(
        "matrix",
        help="emit free-threaded wheel jobs for a release workflow (a stopgap)",
        description=(
            "Prints a GitHub Actions workflow building free-threaded wheels: explicit t "
            "interpreters on manylinux/musllinux, macOS and Windows. A stopgap: check what "
            "`maturin generate-ci github` emits for your project first."
        ),
    )
    matrix_parser.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    matrix_parser.add_argument(
        "--python",
        action="append",
        default=[],
        metavar="VERSION",
        help="free-threaded interpreter, e.g. 3.14t; repeatable (default: 3.14t and 3.15t)",
    )
    matrix_parser.add_argument(
        "--platform",
        action="append",
        default=[],
        help="platform to build for; repeatable (default: manylinux x86_64/aarch64, "
        "musllinux x86_64, macOS arm64, Windows x86_64)",
    )
    matrix_parser.add_argument(
        "--abi3t",
        action="store_true",
        help="also emit a disabled PEP 803 abi3t job, to enable once your maturin and PyO3 "
        "versions build abi3t wheels",
    )
    matrix_parser.add_argument("--output", metavar="FILE", help="write here instead of stdout")

    for name, (summary, _phase, _detail) in _NOT_IMPLEMENTED.items():
        subparsers.add_parser(name, help=f"{summary} (not implemented)")

    return parser


def _add_sanitizer_arguments(p: argparse.ArgumentParser, work_name: str) -> None:
    """The options `ci` and `stress` share: where and how to build under TSan."""
    p.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    p.add_argument(
        "--python",
        default=os.environ.get("FTCHECK_PYTHON", "python3"),
        help="the TSan-instrumented free-threaded interpreter (default: $FTCHECK_PYTHON or python3)",
    )
    p.add_argument(
        "--toolchain",
        default=os.environ.get("FTCHECK_RUST_TOOLCHAIN", "nightly"),
        help="nightly toolchain whose LLVM matches the interpreter's "
        "(default: $FTCHECK_RUST_TOOLCHAIN or nightly)",
    )
    p.add_argument("--threads", type=int, default=8, help="threads (default: 8)")
    p.add_argument(
        "--work-dir",
        help=f"where builds, the venv and logs go (default: <path>/target/{work_name})",
    )
    p.add_argument("--target-dir", help="Cargo target directory (default: in work dir)")
    p.add_argument(
        "--suppressions",
        action="append",
        default=[],
        metavar="FILE",
        help="TSan suppression file; repeatable. $FTCHECK_TSAN_SUPPRESSIONS is always "
        "included when set",
    )
    p.add_argument(
        "--with",
        dest="with_packages",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="also install this package into the venv; repeatable",
    )
    p.add_argument("--check", action="store_true", help="run the environment preflight only")
    p.add_argument("--sarif", metavar="FILE", help="write SARIF 2.1.0 here")
    p.add_argument("--junit", metavar="FILE", help="write JUnit XML here")
    p.add_argument("--format", choices=["text", "json"], default="text", help="stdout format")


def _run_lint(args: argparse.Namespace) -> int:
    root = pathlib.Path(args.path)
    if not root.exists():
        print(f"ftcheck: no such path: {root}", file=sys.stderr)
        return USAGE

    try:
        report = run_lint(
            str(root),
            min_confidence=args.confidence,
            pyo3_version=args.pyo3_version,
        )
    except ImportError as exc:
        print(f"ftcheck: the compiled extension is unavailable: {exc}", file=sys.stderr)
        return UNAVAILABLE
    except ValueError as exc:
        print(f"ftcheck: {exc}", file=sys.stderr)
        return USAGE
    except RuntimeError as exc:
        print(f"ftcheck: {exc}", file=sys.stderr)
        return USAGE

    if report.get("files_scanned", 0) == 0:
        # A lint that examined nothing must not read as a clean one.
        print(
            f"ftcheck: no Rust source found under {root} (scanned 0 .rs files); "
            "nothing was examined",
            file=sys.stderr,
        )
        return UNAVAILABLE

    # Paths relative to the project, as `ci` and `stress` report them: inside
    # the container the absolute path is `/src/...`, which means nothing on
    # the host, and SARIF consumers resolve relative paths against the repo.
    base = root.resolve()
    for finding in report["findings"]:
        path = pathlib.Path(finding["primary"]["file"])
        try:
            finding["primary"]["file"] = str(path.resolve().relative_to(base))
        except ValueError:
            pass

    findings_json = json.dumps(report["findings"])

    if args.sarif or args.junit:
        from ftcheck import _ftcheck

        if args.sarif:
            pathlib.Path(args.sarif).write_text(
                _ftcheck.to_sarif(findings_json, __version__)
            )
        if args.junit:
            pathlib.Path(args.junit).write_text(_ftcheck.to_junit(findings_json))

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        text.render(report, sys.stdout, min_confidence=args.confidence)

    return FINDINGS if report["findings"] else CLEAN


def _write_reports(findings: list, sarif: str | None, junit: str | None) -> None:
    if not (sarif or junit):
        return
    from ftcheck import _ftcheck

    findings_json = json.dumps(findings)
    if sarif:
        pathlib.Path(sarif).write_text(_ftcheck.to_sarif(findings_json, __version__))
    if junit:
        pathlib.Path(junit).write_text(_ftcheck.to_junit(findings_json))


def _sanitizer_setup(args: argparse.Namespace, work_name: str):
    """Validate the shared options. Returns (options, suppressions) or an exit code."""
    from ftcheck.ci.pipeline import Options

    project = pathlib.Path(args.path).resolve()
    if not project.is_dir():
        print(f"ftcheck: no such directory: {project}", file=sys.stderr)
        return USAGE
    if args.threads < 2:
        print("ftcheck: --threads must be at least 2; one thread cannot race", file=sys.stderr)
        return USAGE

    suppressions = list(args.suppressions)
    if os.environ.get("FTCHECK_TSAN_SUPPRESSIONS"):
        suppressions.insert(0, os.environ["FTCHECK_TSAN_SUPPRESSIONS"])
    for path in suppressions:
        if not pathlib.Path(path).is_file():
            print(f"ftcheck: no such suppression file: {path}", file=sys.stderr)
            return USAGE

    work = pathlib.Path(args.work_dir) if args.work_dir else project / "target" / work_name
    return Options(
        project=project,
        work_dir=work.resolve(),
        threads=args.threads,
        tests=getattr(args, "tests", []),
        target_dir=pathlib.Path(args.target_dir).resolve() if args.target_dir else None,
        suppressions=suppressions,
        extras=getattr(args, "extra", []),
        pip_args=args.with_packages,
        pytest_args=getattr(args, "pytest_arg", []),
        test_deps=not getattr(args, "no_test_deps", False),
    )


def _run_ci(args: argparse.Namespace) -> int:
    from ftcheck.ci import verdict
    from ftcheck.ci.environment import probe
    from ftcheck.ci.pipeline import run

    opts = _sanitizer_setup(args, "ftcheck-ci")
    if isinstance(opts, int):
        return opts

    env = probe(args.python, args.toolchain)
    outcome = run(env, opts) if env.ok and not args.check else None

    if args.check and env.ok:
        code, headline = CLEAN, "environment ready; nothing was built or run"
    else:
        result = verdict(env, outcome)
        code, headline = result.code, result.headline

    _write_reports(outcome.findings if outcome else [], args.sarif, args.junit)

    if args.format == "json":
        print(json.dumps(text.ci_json(env, outcome, code, headline, args.threads), indent=2))
    else:
        text.render_ci(env, outcome, code, headline, args.threads, sys.stdout)
    return code


def _run_stress(args: argparse.Namespace) -> int:
    from ftcheck import stress
    from ftcheck.ci.environment import probe

    opts = _sanitizer_setup(args, "ftcheck-stress")
    if isinstance(opts, int):
        return opts
    if args.budget <= 0 or args.iterations < 1:
        print("ftcheck: --budget and --iterations must be positive", file=sys.stderr)
        return USAGE
    try:
        config = stress.load_config(opts.project)
    except (ValueError, OSError) as exc:
        print(f"ftcheck: {exc}", file=sys.stderr)
        return USAGE

    seed = args.replay if args.replay is not None else args.seed
    sopts = stress.StressOptions(
        seed=seed if seed is not None else stress.default_seed(),
        budget_seconds=args.budget,
        iterations=args.iterations,
        only=args.only,
        modules=args.module,
    )

    env = probe(args.python, args.toolchain)
    outcome = stress.run(env, opts, sopts, config) if env.ok and not args.check else None

    if args.check and env.ok:
        code, headline = CLEAN, "environment ready; nothing was built or run"
    else:
        result = stress.verdict(env, outcome)
        code, headline = result.code, result.headline
        driven, total = stress.coverage(outcome.result if outcome else None)
        if code == CLEAN and args.min_coverage and total:
            pct = 100.0 * driven / total
            if pct < args.min_coverage:
                code = UNAVAILABLE
                headline = (
                    f"could not run adequately: {pct:.0f}% of the surface driven, below "
                    f"--min-coverage {args.min_coverage:g}% ({headline})"
                )

    _write_reports(outcome.findings if outcome else [], args.sarif, args.junit)

    if args.format == "json":
        report = text.ci_json(env, outcome, code, headline, args.threads)
        report["stress"] = outcome.result if outcome else None
        report["seed"] = sopts.seed
        driven, total = stress.coverage(report["stress"])
        report["coverage"] = {
            "driven": driven,
            "total": total,
            "confined": stress.confined(report["stress"]),
        }
        print(json.dumps(report, indent=2))
    else:
        replay = (
            f"ftcheck stress {args.path} --replay {sopts.seed} --threads {opts.threads} "
            f"--iterations {sopts.iterations} --budget {sopts.budget_seconds:g}"
            + "".join(f" --only {o}" for o in sopts.only)
            + "".join(f" --module {m}" for m in sopts.modules)
        )
        text.render_stress(
            env, outcome, code, headline, sopts, opts.threads, sys.stdout, replay=replay
        )
    return code


def _run_matrix(args: argparse.Namespace) -> int:
    from ftcheck import matrix

    project = pathlib.Path(args.path)
    if not project.is_dir():
        print(f"ftcheck: no such directory: {project}", file=sys.stderr)
        return USAGE
    manifest, notes = matrix.load_project(project)
    spec = matrix.Matrix(
        pythons=args.python or list(matrix.DEFAULT_PYTHONS),
        platforms=args.platform or list(matrix.DEFAULT_PLATFORMS),
        abi3t=args.abi3t,
        manifest_path=manifest,
        notes=notes,
    )
    try:
        text_out = matrix.render(spec)
    except ValueError as exc:
        print(f"ftcheck: {exc}", file=sys.stderr)
        return USAGE
    if args.output:
        pathlib.Path(args.output).write_text(text_out)
    else:
        sys.stdout.write(text_out)
    return CLEAN


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code in (None, 0):
            return CLEAN
        return USAGE

    if args.command == "lint":
        return _run_lint(args)
    if args.command == "ci":
        return _run_ci(args)
    if args.command == "stress":
        return _run_stress(args)
    if args.command == "matrix":
        return _run_matrix(args)

    summary, phase, detail = _NOT_IMPLEMENTED[args.command]
    print(
        f"ftcheck: `{args.command}` is not implemented — {summary}, {phase}.\n"
        f"  {detail}\n"
        f"  Exiting {UNAVAILABLE} (could not run), not {CLEAN} (clean): "
        f"a command that did not run must never look like a passing one.",
        file=sys.stderr,
    )
    return UNAVAILABLE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

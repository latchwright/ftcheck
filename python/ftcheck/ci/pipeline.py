# SPDX-License-Identifier: MIT OR Apache-2.0
"""The sanitizer pipeline: instrumented build, isolated install, suite run.

Runs inside an environment that already has a TSan-instrumented free-threaded
CPython — the ftcheck-tsan image, or a CI job using it. It never writes into
the project: the Cargo target directory, the venv, the wheel and the TSan logs
all live under the work directory.
"""
from __future__ import annotations

import glob
import os
import pathlib
import re
import shlex
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

from ftcheck.ci import tsan
from ftcheck.ci.environment import Environment

__all__ = [
    "Options",
    "Outcome",
    "Prepared",
    "collect",
    "native_modules",
    "prepare",
    "run",
    "runtime_env",
]

DEFAULT_SUPPRESSIONS = pathlib.Path(__file__).with_name("suppressions") / "rust.supp"

# The test runner's own dependencies. pytest-run-parallel is what makes an
# ordinary suite concurrent: it runs every test body in N threads at once.
_RUNNER_DEPS = ("pytest", "pytest-run-parallel")


@dataclass
class Options:
    project: pathlib.Path
    work_dir: pathlib.Path
    threads: int = 8
    tests: list[str] = field(default_factory=list)
    target_dir: pathlib.Path | None = None
    suppressions: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    pip_args: list[str] = field(default_factory=list)
    pytest_args: list[str] = field(default_factory=list)
    test_deps: bool = True


@dataclass
class Outcome:
    """What happened. `stage` is where it stopped; `error` says why."""

    stage: str = "preflight"
    error: str | None = None
    log: str | None = None
    extension_modules: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    reached: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    log_path: str | None = None
    pytest_summary: str | None = None
    aborted: str | None = None
    external: list[dict] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    tests_collected: int = 0
    tests_failed: int = 0
    tests_errored: int = 0
    pytest_exit: int | None = None


def _log(message: str) -> None:
    print(f"ftcheck: {message}", file=sys.stderr, flush=True)


def _stream(cmd: list[str], log_path: pathlib.Path, **kwargs) -> int:
    """Run a command, teeing its output into a log file the user can read."""
    with open(log_path, "w") as log:
        log.write("$ " + shlex.join(cmd) + "\n")
        log.flush()
        return subprocess.run(
            cmd, stdout=log, stderr=subprocess.STDOUT, check=False, **kwargs
        ).returncode


def _tail(path: pathlib.Path, lines: int = 25) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _tsan_options(env: Environment, log_prefix: pathlib.Path, suppressions: list[str]) -> str:
    """TSan runtime options. The user's own options come first; ours win."""
    options = [os.environ.get("TSAN_OPTIONS", "").strip()]
    options += [
        "halt_on_error=0",
        # Reports are counted from the log files, never from the exit status.
        "exitcode=0",
        "second_deadlock_stack=1",
        f"log_path={log_prefix}",
    ]
    if env.symbolizer:
        options.append(f"external_symbolizer_path={env.symbolizer}")
    # TSan accepts one suppressions file, so several are concatenated. ftcheck's
    # own defaults come first; each entry there carries its justification.
    # FTCHECK_NO_DEFAULT_SUPPRESSIONS=1 turns them off, to audit what they hide.
    if not os.environ.get("FTCHECK_NO_DEFAULT_SUPPRESSIONS"):
        suppressions = [str(DEFAULT_SUPPRESSIONS), *suppressions]
    if suppressions:
        merged = log_prefix.parent / "suppressions.txt"
        with open(merged, "w") as out:
            for path in suppressions:
                out.write(f"# --- {path}\n")
                out.write(pathlib.Path(path).read_text())
                out.write("\n")
        options.append(f"suppressions={merged}")
    return " ".join(o for o in options if o)


def _extension_modules(wheel: pathlib.Path) -> list[str]:
    with zipfile.ZipFile(wheel) as zf:
        return sorted(
            {os.path.basename(n) for n in zf.namelist() if n.endswith((".so", ".pyd"))}
        )


def native_modules(wheel: pathlib.Path) -> list[str]:
    """Dotted import names of the wheel's extension modules.

    `pkg/_native.cpython-314t-x86_64-linux-gnu.so` -> `pkg._native`.
    """
    names = set()
    with zipfile.ZipFile(wheel) as zf:
        for n in zf.namelist():
            if n.endswith((".so", ".pyd")) and ".data/" not in n:
                stem = n.rsplit("/", 1)
                module = stem[-1].split(".", 1)[0]
                package = stem[0].replace("/", ".") if len(stem) == 2 else ""
                names.add(f"{package}.{module}" if package else module)
    return sorted(names)


# Libraries every extension links that come from the toolchain or libc, not
# from the project: nothing to instrument there.
_SYSTEM_BASELINE = (
    "libc.so", "libm.so", "libpthread.so", "libdl.so", "librt.so", "libgcc_s.so",
    "ld-linux", "libutil.so", "libpython", "libstdc++.so",
)  # fmt: skip


def _uninstrumented_libraries(wheel: pathlib.Path, work: pathlib.Path) -> list[str]:
    """Warnings for shared libraries the extension links but TSan cannot see into.

    A C library found through pkg-config links the system's uninstrumented copy
    instead of building the vendored one under -fsanitize=thread. Races inside
    it can be missed, so say so.
    """
    import shutil

    readelf = shutil.which("readelf")
    if not readelf:
        return []
    out_dir = work / "inspect"
    out_dir.mkdir(exist_ok=True)
    warnings = []
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            if not name.endswith(".so"):
                continue
            target = out_dir / os.path.basename(name)
            target.write_bytes(zf.read(name))
            if _stripped(target):
                warnings.append(
                    f"{os.path.basename(name)} is stripped, so every frame of yours in a report "
                    "will be nameless (`<null>`). Set `strip = false` under [tool.maturin] in "
                    "pyproject.toml (and [profile.release] in Cargo.toml) for ftcheck runs."
                )
            dynamic = subprocess.run(
                [readelf, "-d", str(target)], capture_output=True, text=True, check=False
            ).stdout
            for lib in re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", dynamic):
                if not lib.startswith(_SYSTEM_BASELINE):
                    warnings.append(
                        f"{os.path.basename(name)} links the system library {lib}, which was not "
                        "rebuilt with -fsanitize=thread: races inside it can be missed. If the "
                        "crate can build a vendored copy, prefer that (often an environment "
                        "variable such as <LIB>_NO_PKG_CONFIG=1)."
                    )
    return warnings


_COLLECTED = re.compile(r"[Cc]ollected (\d+) items?")
_SUMMARY = re.compile(r"^=+ (.*(?:passed|failed|error|no tests ran).*) =+$", re.MULTILINE)


def _read_session(out: Outcome, log: pathlib.Path) -> None:
    """pytest's own summary line, and whether the session aborted.

    A session that dies part-way (an INTERNALERROR, seen on a public project)
    must not read as a suite that ran and failed.
    """
    try:
        text = log.read_text(errors="replace")
    except OSError:
        return
    summaries = _SUMMARY.findall(text)
    out.pytest_summary = summaries[-1].strip() if summaries else None
    collected = [int(n) for n in _COLLECTED.findall(text)]
    if "INTERNALERROR" in text:
        of = f" of {max(collected)} collected" if collected else ""
        out.aborted = f"pytest aborted with an INTERNALERROR after {out.tests_collected} tests{of}"


def _build_backend(project: pathlib.Path) -> str | None:
    """Why this project cannot be built by maturin as it stands, or None.

    ftcheck builds with maturin. A setuptools-rust project built that way
    silently lacked its bindings (a public project); say so instead.
    """
    import tomllib

    pyproject = project / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    backend = data.get("build-system", {}).get("build-backend", "")
    if "maturin" in backend or "maturin" in data.get("tool", {}):
        return None
    if backend.startswith("setuptools") and (project / "Cargo.toml").is_file():
        return (
            "this project builds its extension with setuptools-rust; ftcheck builds with "
            "maturin. Add a [tool.maturin] section to pyproject.toml naming the module and "
            "any Cargo features, e.g. module-name = \"pkg._native\" and "
            "features = [\"python\"] (see docs/ci.md)"
        )
    return None


def _build_requirements(project: pathlib.Path) -> list[str]:
    """`[build-system].requires`, less maturin itself.

    A build script can import Python packages (cffi and setuptools, on a
    public project), and nothing installed them. maturin is left out: the
    image's own binary builds, and a pip-installed one would shadow it.
    """
    import tomllib

    try:
        data = tomllib.loads((project / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    requires = data.get("build-system", {}).get("requires", [])
    kept = []
    for req in requires:
        name = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
        if name and re.sub(r"[-_.]+", "-", name.group(1)).lower() == "maturin":
            continue
        kept.append(req)
    return kept


def _stripped(path: pathlib.Path) -> bool:
    import shutil

    readelf = shutil.which("readelf")
    if not readelf:
        return False
    sections = subprocess.run(
        [readelf, "-S", str(path)], capture_output=True, text=True, check=False
    ).stdout
    # No section headers at all means readelf could not read it: not stripped.
    return "Section Headers" in sections and ".symtab" not in sections


def _count_tests(junit: pathlib.Path) -> tuple[int, int, int]:
    """(collected, failed, errored) from pytest's JUnit XML."""
    try:
        root = ElementTree.parse(junit).getroot()
    except (OSError, ElementTree.ParseError):
        return 0, 0, 0
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    return tests - skipped, failed, errors


# Printed True only on a free-threaded build whose GIL came back on import.
_GIL_PROBE = (
    "import importlib, sys, sysconfig\n"
    "importlib.import_module(sys.argv[1])\n"
    "print(bool(sysconfig.get_config_var('Py_GIL_DISABLED')) and sys._is_gil_enabled())\n"
)


def _reenables_gil(python: str, module: str, env: dict, cwd) -> bool | None:
    """Whether importing `module` turns the GIL back on; None if the probe failed.

    Run without PYTHON_GIL, which is how the module's users will import it.
    """
    probe_env = {k: v for k, v in env.items() if k != "PYTHON_GIL"}
    try:
        proc = subprocess.run(
            [python, "-c", _GIL_PROBE, module],
            capture_output=True, text=True, env=probe_env, cwd=cwd, timeout=300, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    last = proc.stdout.strip().splitlines()[-1:]
    if proc.returncode != 0 or not last:
        return None
    return {"True": True, "False": False}.get(last[0])


def _gil_notes(python: str, modules: list[str], env: dict, cwd) -> list[str]:
    return [
        f"`{module}` does not declare `gil_used = false`, so a free-threaded interpreter "
        "re-enables the GIL when it is imported. This run forced the GIL off "
        "(PYTHON_GIL=0): its results describe the module only when it is imported that "
        "way. Declare `#[pymodule(gil_used = false)]` once the module is thread-safe."
        for module in modules
        if _reenables_gil(python, module, env, cwd)
    ]


def _make_venv(env: Environment, venv: pathlib.Path, log: pathlib.Path, setup_env: dict) -> bool:
    """A venv on the TSan interpreter, unless one is already there."""
    if (venv / "bin" / "python").exists():
        return True
    _log(f"creating {venv.name}")
    cmd = [env.interpreter["executable"], "-m", "venv", str(venv)]
    return _stream(cmd, log, env=setup_env) == 0


@dataclass
class Prepared:
    """An instrumented build installed into a venv, ready to be exercised."""

    python: str
    wheel: pathlib.Path
    tsan_dir: pathlib.Path


def prepare(
    env: Environment, opts: Options, out: Outcome, install_runner: bool = True
) -> Prepared | None:
    """Stages 1 and 2: instrumented build, isolated install. None on failure.

    `install_runner=False` skips pytest and pytest-run-parallel, which only `ci`
    uses: `stress` runs no tests, and every download is one more thing a flaky
    network can stall.
    """
    work = opts.work_dir
    work.mkdir(parents=True, exist_ok=True)
    wheels = work / "wheels"
    for old in wheels.glob("*.whl"):
        old.unlink()
    target_dir = opts.target_dir or work / "target"
    # --- 1. instrumented build ------------------------------------------------
    out.stage = "build"
    backend = _build_backend(opts.project)
    if backend is not None:
        out.error = backend
        return None
    build_env = {
        **os.environ,
        "RUSTUP_TOOLCHAIN": env.toolchain,
        # crossbeam (and so rayon) switches to orderings TSan can model under
        # this cfg — its source: "ThreadSanitizer does not understand fences".
        # Without it, rayon's deque reports a race that is not one (seen on a
        # public project).
        "RUSTFLAGS": (
            os.environ.get("RUSTFLAGS", "") + " -Zsanitizer=thread --cfg crossbeam_sanitize_thread"
        ).strip(),
        "CARGO_TARGET_DIR": str(target_dir),
        # File and line for every Rust frame, without full debuginfo's cost.
        "CARGO_PROFILE_RELEASE_DEBUG": "line-tables-only",
        # A project's `strip = true` would erase every frame name from the
        # reports (seen on two public projects).
        "CARGO_PROFILE_RELEASE_STRIP": "false",
        # ...and so would `strip = true` under [tool.maturin], which maturin
        # applies itself; since 1.12 this variable overrides pyproject.toml.
        # An older maturin ignores it, and the stripped-library warning remains.
        "MATURIN_STRIP": "false",
        # C and C++ built by build scripts (the `cc` crate honours these) must be
        # instrumented too, and by the clang that built the interpreter: gcc's
        # libtsan is a different runtime from the one already loaded.
        "CFLAGS": (os.environ.get("CFLAGS", "") + " -fsanitize=thread -g").strip(),
        "CXXFLAGS": (os.environ.get("CXXFLAGS", "") + " -fsanitize=thread -g").strip(),
    }
    # Setup runs under TSan too — it is the same interpreter — but its reports
    # are CPython's and pip's, not the project's, so they go to a separate log.
    setup_env = {
        **os.environ,
        "TSAN_OPTIONS": _tsan_options(env, work / "setup-tsan", opts.suppressions),
    }
    build_requires = _build_requirements(opts.project)
    if build_requires:
        # A venv of their own, on PATH for the build: the test venv then holds
        # only what the project declares for its tests.
        build_venv = work / "build-venv"
        deps_log = work / "build-deps.log"
        _log(f"installing the build requirements ({', '.join(build_requires)})")
        if not _make_venv(env, build_venv, deps_log, setup_env):
            out.error = "could not create a venv for the build requirements"
            out.log = _tail(deps_log)
            return None
        cmd = [
            str(build_venv / "bin" / "python"), "-m", "pip", "install", "--quiet",
            "--disable-pip-version-check", *build_requires,
        ]  # fmt: skip
        if _stream(cmd, deps_log, env=setup_env) != 0:
            out.error = "could not install the project's [build-system].requires"
            out.log = _tail(deps_log)
            out.log_path = str(deps_log)
            return None
        build_env["PATH"] = os.pathsep.join(
            [str(build_venv / "bin"), os.environ.get("PATH", os.defpath)]
        )
    if env.cc:
        build_env["CC"] = env.cc
    if env.cxx:
        build_env["CXX"] = env.cxx
    build_cmd = [
        "maturin", "build", "--release",
        "--interpreter", env.interpreter["executable"],
        "--out", str(wheels),
        # panic_abort too, so a crate whose release profile sets
        # `panic = "abort"` builds (found on a public project); harmless otherwise.
        "-Zbuild-std=std,panic_abort",
        "--target", env.target or "x86_64-unknown-linux-gnu",
        # This wheel never leaves the machine; manylinux repair needs patchelf,
        # which the image lacks, and would rewrite the very library under test.
        "--auditwheel", "skip",
    ]  # fmt: skip
    _log(f"building {opts.project} with ThreadSanitizer ({env.toolchain}, -Zbuild-std)")
    build_log = work / "build.log"
    if _stream(build_cmd, build_log, cwd=opts.project, env=build_env) != 0:
        out.error = "the instrumented build failed"
        out.log = _tail(build_log)
        return None
    built = sorted(wheels.glob("*.whl"))
    if len(built) != 1:
        out.error = f"expected one wheel from the build, found {len(built)}"
        return None
    wheel = built[0]
    out.extension_modules = _extension_modules(wheel)
    if not out.extension_modules:
        out.error = f"{wheel.name} contains no extension module"
        return None
    out.warnings.extend(_uninstrumented_libraries(wheel, work))

    # --- 2. an isolated venv on the TSan interpreter ---------------------------
    out.stage = "install"
    venv = work / "venv"
    tsan_dir = work / "tsan"
    tsan_dir.mkdir(exist_ok=True)
    # The previous run's raw logs move aside instead of being deleted — a rerun
    # in the same work directory once erased the only logs of a confirmed race.
    previous = work / "tsan-previous"
    old_logs = [f for f in tsan_dir.glob("*") if f.is_file()]
    if old_logs:
        if previous.exists():
            for f in previous.glob("*"):
                f.unlink()
        previous.mkdir(exist_ok=True)
        for f in old_logs:
            f.rename(previous / f.name)
    install_log = work / "install.log"
    python = str(venv / "bin" / "python")
    if not _make_venv(env, venv, install_log, setup_env):
        out.error = "could not create a venv on the TSan interpreter"
        out.log = _tail(install_log)
        return None
    spec = str(wheel) + (f"[{','.join(opts.extras)}]" if opts.extras else "")
    _log(f"installing {wheel.name} and the test runner")
    cmd = [
        python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
        "--force-reinstall", "--no-deps", str(wheel),
    ]  # fmt: skip
    if _stream(cmd, install_log, env=setup_env) != 0:
        out.error = "could not install the instrumented wheel"
        out.log = _tail(install_log)
        return None
    cmd = [
        python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
        spec, *(_RUNNER_DEPS if install_runner else ()), *opts.pip_args,
    ]  # fmt: skip
    if _stream(cmd, work / "install-deps.log", env=setup_env) != 0:
        out.error = "could not install test dependencies"
        out.log = _tail(work / "install-deps.log")
        out.log_path = str(work / "install-deps.log")
        return None

    if install_runner and opts.test_deps:
        declared = _declared_test_dependencies(opts.project, _select_tests(opts)[0])
        if declared:
            label, extra_args = declared
            if extra_args and extra_args[0] == "{wheel}":
                extra_args = [f"{wheel}[{extra_args[1]}]"]
            _log(f"installing the project's test dependencies ({label})")
            cmd = [
                python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                *extra_args,
            ]  # fmt: skip
            log = work / "install-test-deps.log"
            if _stream(cmd, log, cwd=opts.project, env=setup_env) != 0:
                out.error = f"could not install the project's test dependencies ({label})"
                out.log = _tail(log)
                out.log_path = str(log)
                return None
            out.notes.append(f"installed the project's test dependencies from {label}")

    # Every run forces PYTHON_GIL=0; say when that hides what users would get.
    out.notes.extend(
        _gil_notes(python, native_modules(wheel), {**setup_env, "PYTHONSAFEPATH": "1"}, work)
    )
    return Prepared(python=python, wheel=wheel, tsan_dir=tsan_dir)


def _pytest_testpaths(project: pathlib.Path) -> tuple[list[str], str] | None:
    """`testpaths` from the project's pytest configuration, and the file it is in.

    Files are tried in pytest's own order, and the first one that configures
    pytest decides, even if it sets no `testpaths`.
    """
    import configparser
    import tomllib

    for name, section in (
        ("pytest.ini", "pytest"), (".pytest.ini", "pytest"), ("pyproject.toml", None),
        ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest"),
    ):  # fmt: skip
        path = project / name
        if not path.is_file():
            continue
        if section is None:
            try:
                tool = tomllib.loads(path.read_text()).get("tool", {})
            except (OSError, tomllib.TOMLDecodeError):
                continue
            table = tool.get("pytest")
            if not isinstance(table, dict):
                continue
            # pytest 9's native `[tool.pytest]`, or the older `ini_options`.
            table = table.get("ini_options", table)
            value = table.get("testpaths")
        else:
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(path)
            except (OSError, configparser.Error):
                continue
            if not parser.has_section(section):
                continue
            value = parser.get(section, "testpaths", fallback=None)
        if isinstance(value, str):
            value = value.split()
        return list(value or []), name
    return None


def _select_tests(opts: Options) -> tuple[list[str], str]:
    """The test paths to run, and why these: `--tests`, pytest's `testpaths`,
    `tests/`, else the project root."""
    if opts.tests:
        return list(opts.tests), "--tests"
    configured = _pytest_testpaths(opts.project)
    if configured:
        patterns, source = configured
        # pytest expands globs in testpaths and ignores entries that match nothing.
        paths = []
        for pattern in patterns:
            found = sorted(glob.glob(pattern, root_dir=opts.project))
            paths += [p for p in found if p not in paths]
        if paths:
            return paths, f"testpaths in {source}"
    if (opts.project / "tests").is_dir():
        return ["tests"], "tests/ directory"
    return ["."], "no tests/ directory"


def _declared_test_dependencies(
    project: pathlib.Path, tests: list[str] | None = None
) -> tuple[str, list[str]] | None:
    # Returns (label, pip arguments); ["{wheel}", extra] means "the built wheel
    # with this extra", resolved by the caller, which knows the wheel.
    """Where the project declares its test dependencies, as pip arguments.

    Neither project in a first-user trial got past test collection: the venv
    held only the wheel and the runner. In order: a PEP 735 dependency group
    (`test`, `tests`, `testing`), an optional-dependencies extra of those
    names, a `requirements.txt` beside the selected tests (`tests` defaults
    to `tests/`), a conventional requirements file at the root; `dev` last,
    as it often carries whole toolchains. `--no-test-deps` turns this off.
    """
    import tomllib

    names = ("test", "tests", "testing")
    data: dict = {}
    if (project / "pyproject.toml").is_file():
        try:
            data = tomllib.loads((project / "pyproject.toml").read_text())
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
    groups = data.get("dependency-groups", {})
    extras = data.get("project", {}).get("optional-dependencies", {})
    for name in names:
        if name in groups:
            return f"dependency group '{name}'", ["--group", name]
    for name in names:
        if name in extras:
            # Applied to the instrumented wheel, never `.[name]`: installing
            # from the source tree would build a second, uninstrumented copy.
            return f"extra '{name}'", ["{wheel}", name]
    # Beside the tests being run, never a `tests/` that is not among them.
    beside = []
    for test in tests if tests is not None else ["tests"]:
        folder = test if (project / test).is_dir() else os.path.dirname(test)
        rel = os.path.normpath(os.path.join(folder, "requirements.txt"))
        # The root's requirements.txt is the package's own, not its tests'.
        if os.path.dirname(rel) and rel not in beside:
            beside.append(rel)
    for rel in (*beside, "requirements-test.txt", "requirements-tests.txt",
                "test-requirements.txt", "requirements/test.txt", "requirements/tests.txt",
                "requirements-dev.txt", "requirements/dev.txt"):  # fmt: skip
        if (project / rel).is_file():
            return rel, ["-r", rel]
    if "dev" in groups:
        return "dependency group 'dev'", ["--group", "dev"]
    return None


def runtime_env(env: Environment, opts: Options, tsan_dir: pathlib.Path) -> dict:
    """The environment code under test runs in."""
    return {
        **os.environ,
        "TSAN_OPTIONS": _tsan_options(env, tsan_dir / "tsan", opts.suppressions),
        # A module that does not declare `gil_used = false` — the default on
        # PyO3 < 0.28 — re-enables the GIL on import, and with the GIL on TSan
        # sees no race at all. Force it off: the question is what happens
        # without it.
        "PYTHON_GIL": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Keep the current directory off sys.path, so an uninstalled source
        # package in the project root cannot shadow the instrumented wheel —
        # common in maturin mixed layouts, and a first user's blocker.
        "PYTHONSAFEPATH": "1",
    }


def _source_root(project: pathlib.Path) -> pathlib.Path:
    """The Cargo workspace root, when the project is a member of one.

    A frame in a sibling workspace crate (`crates/example-core/...`) is the
    project's own code, not a dependency's (a public project's primary location
    skipped it for that reason).
    """
    project = project.resolve()
    for candidate in [project, *project.parents]:
        manifest = candidate / "Cargo.toml"
        try:
            if manifest.is_file() and "[workspace]" in manifest.read_text():
                return candidate
        except OSError:
            continue
    return project


def collect(out: Outcome, tsan_dir: pathlib.Path, project: pathlib.Path, extra: str = "") -> None:
    """Stage 4: parse, attribute and deduplicate every TSan log."""
    out.stage = "collect"
    text = "\n".join(
        pathlib.Path(p).read_text(errors="replace") for p in sorted(glob.glob(f"{tsan_dir}/tsan*"))
    )
    out.fatal = tsan.fatal_errors(text + "\n" + extra)
    out.findings, out.reached, out.external = tsan.to_findings(
        tsan.parse(text), set(out.extension_modules), str(_source_root(project))
    )
    out.stage = "done"


def run(env: Environment, opts: Options) -> Outcome:
    """`ftcheck ci`: prepare, then the project's suite with every test in N threads."""
    out = Outcome()
    prepared = prepare(env, opts, out)
    if prepared is None:
        return out
    work = opts.work_dir

    out.stage = "test"
    junit = work / "pytest-junit.xml"
    tests, chosen = _select_tests(opts)
    out.notes.append(f"running {' '.join(tests)} ({chosen})")
    cmd = [
        prepared.python, "-m", "pytest", *tests,
        f"--parallel-threads={opts.threads}",
        "--import-mode=importlib",
        "-p", "no:cacheprovider",
        f"--junitxml={junit}",
        "-q",
        *opts.pytest_args,
    ]  # fmt: skip
    _log(f"running {' '.join(tests)} under pytest-run-parallel with {opts.threads} threads")
    test_log = work / "pytest.log"
    out.log_path = str(test_log)
    out.pytest_exit = _stream(
        cmd, test_log, cwd=opts.project, env=runtime_env(env, opts, prepared.tsan_dir)
    )
    out.log = _tail(test_log)
    out.tests_collected, out.tests_failed, out.tests_errored = _count_tests(junit)
    _read_session(out, test_log)

    collect(out, prepared.tsan_dir, opts.project, test_log.read_text(errors="replace"))
    return out

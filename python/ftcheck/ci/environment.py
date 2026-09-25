# SPDX-License-Identifier: MIT OR Apache-2.0
"""Preflight: refuse to run where the result would be silently wrong.

Every check here exists because its failure does not crash — it produces a
quiet, clean-looking run that exercised nothing, or that could not have seen
the race. A preflight failure is exit 3, "could not run", and names the fix.

The gotchas, and where each is enforced:

1. CPython's LLVM major must match nightly Rust's, or races hide.  `llvm_match`
2. 3.14t, not 3.13t, or reports drown in CPython's own races.    `python_version` (warning)
3. Never `TSAN_OPTIONS=force_seq_cst_atomics=1`; it masks races.  `tsan_options`
4. The host's ASLR entropy, which no image can lower.             `interpreter`
5. std must be rebuilt instrumented: `rust-src` + `-Zbuild-std`.   `rust_src`
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

__all__ = ["Check", "Environment", "probe"]

_PROBE = r"""
import json, sys, sysconfig
cfg = sysconfig.get_config_vars()
print(json.dumps({
    "executable": sys.executable,
    "version": list(sys.version_info[:3]),
    "version_string": sys.version.split()[0],
    "gil_disabled": bool(cfg.get("Py_GIL_DISABLED")),
    "config_args": cfg.get("CONFIG_ARGS") or "",
    "cflags": " ".join(str(cfg.get(k) or "") for k in ("CFLAGS", "PY_CFLAGS", "CONFIGURE_CFLAGS")),
    "cc": cfg.get("CC") or "",
}))
"""

_ASLR_HINT = (
    "TSan could not start because the host's ASLR entropy is too high "
    "(vm.mmap_rnd_bits > 28). Either run the container with "
    "`--security-opt seccomp=unconfined`, which lets TSan disable ASLR for its own "
    "process, or set `sudo sysctl -w vm.mmap_rnd_bits=28` on the host. No image can "
    "change this for you."
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warning: bool = False  # a problem worth saying, not worth refusing over


@dataclass
class Environment:
    python: str
    toolchain: str
    checks: list[Check] = field(default_factory=list)
    interpreter: dict = field(default_factory=dict)
    llvm_major: str | None = None
    cc: str | None = None
    cxx: str | None = None
    symbolizer: str | None = None
    target: str | None = None

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if not c.warning)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.warning]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.warning]


def _run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)


def _clang_major(cc: str) -> tuple[str | None, str | None]:
    """(major version, resolved compiler) for a clang named by sysconfig's CC."""
    name = cc.split()[0] if cc else ""
    match = re.search(r"clang-(\d+)$", name)
    if match and shutil.which(name):
        return match.group(1), name
    if name and shutil.which(name):
        out = _run([name, "--version"]).stdout
        version = re.search(r"clang version (\d+)", out)
        if version:
            return version.group(1), name
    if match:
        return match.group(1), None
    return None, None


def _find_symbolizer(llvm_major: str | None) -> str | None:
    candidates = []
    if os.environ.get("TSAN_SYMBOLIZER_PATH"):
        candidates.append(os.environ["TSAN_SYMBOLIZER_PATH"])
    if llvm_major:
        candidates += [f"llvm-symbolizer-{llvm_major}", f"/usr/lib/llvm-{llvm_major}/bin/llvm-symbolizer"]
    candidates.append("llvm-symbolizer")
    for c in candidates:
        found = shutil.which(c) or (c if os.path.isfile(c) and os.access(c, os.X_OK) else None)
        if found:
            return found
    return None


def probe(python: str, toolchain: str) -> Environment:
    env = Environment(python=python, toolchain=toolchain)
    add = env.checks.append

    # --- the interpreter ----------------------------------------------------
    try:
        result = _run([python, "-c", _PROBE])
    except FileNotFoundError:
        add(Check("interpreter", False, f"no interpreter at {python!r}"))
        return env
    if "FATAL: ThreadSanitizer" in result.stderr and "ASLR" in result.stderr:
        add(Check("interpreter", False, _ASLR_HINT))
        return env
    if result.returncode != 0:
        add(Check("interpreter", False, f"{python} failed to start: {result.stderr.strip()[:400]}"))
        return env
    info = json.loads(result.stdout.strip().splitlines()[-1])
    env.interpreter = info
    add(Check("interpreter", True, f"{info['executable']} (CPython {info['version_string']})"))

    add(
        Check(
            "free_threaded",
            info["gil_disabled"],
            "free-threaded build"
            if info["gil_disabled"]
            else "not a free-threaded build (Py_GIL_DISABLED is unset); use a `t` interpreter",
        )
    )

    tsan_built = (
        "--with-thread-sanitizer" in info["config_args"] or "-fsanitize=thread" in info["cflags"]
    )
    add(
        Check(
            "tsan_interpreter",
            tsan_built,
            "interpreter built with ThreadSanitizer"
            if tsan_built
            else "interpreter is not TSan-instrumented. Use ghcr.io/nascheme/cpython-tsan:3.14t "
            "(or the ftcheck-tsan image built from docker/Dockerfile).",
        )
    )

    new_enough = tuple(info["version"][:2]) >= (3, 14)
    add(
        Check(
            "python_version",
            new_enough,
            f"CPython {info['version_string']}"
            if new_enough
            else f"CPython {info['version_string']} is older than 3.14: 3.13t carries CPython "
            "races fixed only in 3.14, so expect reports inside the interpreter. Prefer 3.14t.",
            warning=True,
        )
    )

    # --- the C compiler that built it --------------------------------------
    cc_major, cc = _clang_major(info["cc"])
    env.llvm_major, env.cc = cc_major, cc
    if cc_major is None:
        add(
            Check(
                "c_compiler",
                False,
                f"cannot tell which LLVM built this interpreter (CC={info['cc']!r}); "
                "ftcheck needs a clang-built TSan CPython to match Rust against",
            )
        )
    elif cc is None:
        add(
            Check(
                "c_compiler",
                False,
                f"the interpreter was built with clang-{cc_major}, which is not on PATH. "
                "C dependencies must be compiled with the same clang, or their TSan "
                "runtime will not match the interpreter's.",
            )
        )
    else:
        env.cxx = shutil.which(cc.replace("clang", "clang++")) or None
        add(Check("c_compiler", True, f"{cc} (LLVM {cc_major})"))

    # --- the Rust toolchain -------------------------------------------------
    rust_env = {**os.environ, "RUSTUP_TOOLCHAIN": toolchain}
    try:
        rustc = _run(["rustc", "-vV"], env=rust_env)
    except FileNotFoundError:
        add(Check("rust_toolchain", False, "rustc is not on PATH"))
        return env
    if rustc.returncode != 0:
        add(
            Check(
                "rust_toolchain",
                False,
                f"toolchain {toolchain!r} is unavailable: {rustc.stderr.strip()[:300]}. "
                f"Install it with `rustup toolchain install {toolchain} --component rust-src`.",
            )
        )
        return env
    release = re.search(r"^release: (.+)$", rustc.stdout, re.MULTILINE)
    host = re.search(r"^host: (.+)$", rustc.stdout, re.MULTILINE)
    llvm = re.search(r"^LLVM version: (\d+)", rustc.stdout, re.MULTILINE)
    env.target = host.group(1) if host else None
    nightly = bool(release and ("nightly" in release.group(1) or "dev" in release.group(1)))
    add(
        Check(
            "rust_toolchain",
            nightly,
            f"rustc {release.group(1) if release else '?'}"
            if nightly
            else f"rustc {release.group(1) if release else '?'} is not nightly; "
            "-Zsanitizer and -Zbuild-std need one",
        )
    )

    rust_llvm = llvm.group(1) if llvm else None
    if cc_major is not None:
        match = rust_llvm == cc_major
        add(
            Check(
                "llvm_match",
                match,
                f"LLVM {rust_llvm} on both sides"
                if match
                else f"rustc uses LLVM {rust_llvm} but the interpreter was built with LLVM "
                f"{cc_major}. A mismatch does not fail — it hides races (it hid PyO3's own "
                f"BorrowFlag race). Pick a nightly whose `rustc -vV` reports LLVM {cc_major}.",
            )
        )

    sysroot = _run(["rustc", "--print", "sysroot"], env=rust_env).stdout.strip()
    src = os.path.join(sysroot, "lib", "rustlib", "src", "rust", "library")
    has_src = os.path.isdir(src)
    add(
        Check(
            "rust_src",
            has_src,
            "rust-src installed"
            if has_src
            else f"the rust-src component is missing, so std cannot be rebuilt instrumented. "
            f"`rustup component add rust-src --toolchain {toolchain}`",
        )
    )

    maturin = shutil.which("maturin")
    add(
        Check(
            "maturin",
            bool(maturin),
            maturin or "maturin is not on PATH",
        )
    )

    # --- runtime -------------------------------------------------------------
    env.symbolizer = _find_symbolizer(cc_major)
    add(
        Check(
            "symbolizer",
            bool(env.symbolizer),
            env.symbolizer
            or "no llvm-symbolizer found: reports could not be mapped to source lines",
        )
    )

    options = os.environ.get("TSAN_OPTIONS", "")
    masked = re.search(r"(^|[\s:,])force_seq_cst_atomics=(1|true)", options)
    add(
        Check(
            "tsan_options",
            not masked,
            "TSAN_OPTIONS acceptable"
            if not masked
            else "TSAN_OPTIONS sets force_seq_cst_atomics=1, which makes every atomic "
            "sequentially consistent and so hides exactly the ordering races ftcheck looks "
            "for (it masked PyO3's BorrowFlag race). Remove it.",
        )
    )
    return env

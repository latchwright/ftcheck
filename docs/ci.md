# `ftcheck ci` — the ThreadSanitizer pipeline

`ftcheck ci` builds your extension with ThreadSanitizer, installs it into an isolated venv
on a TSan-instrumented free-threaded CPython, runs your test suite with **every test body
in N threads at once**, and reports the races TSan saw.

It does **not** build or ship a sanitizer CPython. It consumes
[`ghcr.io/nascheme/cpython-tsan`](https://github.com/nascheme/cpython_sanity), which is
maintained upstream, rebuilt weekly, and already recommended by the py-free-threading
guide and the PyO3 guide. ftcheck adds only the Rust half that image lacks.

## Quick start

ftcheck is not on PyPI. Build the image from an ftcheck checkout — it carries the Rust
layer and ftcheck itself — then run it against your project:

```console
$ docker build -f docker/Dockerfile -t ftcheck-tsan .        # in the ftcheck checkout
$ cd path/to/your/extension
$ docker run --rm --security-opt seccomp=unconfined \
      --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -v "$PWD":/src ftcheck-tsan ftcheck ci /src --sarif /src/tsan.sarif
```

`--security-opt seccomp=unconfined` is required; see [the host](#the-host-aslr) below.
`--user` keeps the build output under `target/ftcheck-ci/` owned by you rather than root.

## What it does

1. **Preflight.** Checks every condition under which a TSan run is silently wrong, and
   refuses (exit `3`) with the fix named, rather than producing a clean-looking result.
2. **Instrumented build.** `maturin build --release -Zbuild-std` with
   `RUSTFLAGS=-Zsanitizer=thread`, so the standard library is rebuilt instrumented too
   (`panic_abort` included, so crates with `panic = "abort"` build). Symbols are kept even
   if the project strips its release builds, or every frame would be nameless.
   `--cfg crossbeam_sanitize_thread` is set: crossbeam — and so rayon — switches to
   orderings TSan can model ("ThreadSanitizer does not understand fences", in its own
   source), which removes a race report that is not one.
   C and C++ compiled by build scripts get `-fsanitize=thread` through `CFLAGS`/`CXXFLAGS`,
   compiled by **the same clang that built the interpreter**. Line tables are on, so every
   Rust frame has a file and line.
3. **Isolated install.** A venv on the TSan interpreter, with the wheel, `pytest` and
   `pytest-run-parallel`.
4. **The suite, concurrently.** `pytest --parallel-threads=N` (default 8) runs each test
   body in N threads simultaneously. A module-level instance your tests touch is therefore
   **shared across threads** — which is the situation the GIL used to make safe.
   `PYTHON_GIL=0` is forced, so an extension that does not declare `gil_used = false`
   cannot quietly turn the GIL back on and hide every race.
5. **Collect.** TSan logs are parsed, each report is attributed, and repeats of the same
   race — from either side of the pair, from any threads — collapse into one finding.
6. **Report.** Terminal summary, `--format json`, `--sarif`, `--junit`. The lint and the
   sanitizer write one SARIF format; only the rule id (`FT00x` vs `tsan/...`) and the
   `producer` property tell them apart.

## Your test suite under threads

**Test dependencies** are installed automatically when the project declares them: a
[PEP 735](https://peps.python.org/pep-0735/) dependency group named `test`, `tests` or
`testing`; else an optional-dependencies extra of those names (installed on the
instrumented wheel, never from source); else a `requirements` file such as
`tests/requirements.txt`, `requirements-test.txt` or `requirements-dev.txt`; else a `dev`
group. Add anything else with `--with PACKAGE`; turn this off with `--no-test-deps`.

**Tests that are not thread-safe** — using `mocker`, `capsys`, signal handlers, a fixed
port, a shared temporary file — fail when every test body runs in N threads, and the run
exits `4`. Mark them for pytest-run-parallel, which then runs them once, on one thread:

```python
@pytest.mark.thread_unsafe
def test_uses_a_signal_handler(): ...
```

or deselect them: `--pytest-arg=-k --pytest-arg="not network"`. Any pytest argument can
be passed this way.

**A session that aborts** part-way — a pytest `INTERNALERROR` — exits `3`, not `4`: the
suite did not run, so nothing can be said about it. pytest's own summary line is printed.

**Stripped builds.** If the extension is stripped (`strip = true` under `[tool.maturin]` or
in the release profile), every frame of yours in a report is nameless; ftcheck warns. Set
`strip = false` for ftcheck runs.

**setuptools-rust projects** are not built as they stand — ftcheck builds with maturin and
says so. A `[tool.maturin]` section naming the module and any Cargo features is enough.

**Source layouts.** The run sets `PYTHONSAFEPATH=1`, so an uninstalled package in your
project root cannot shadow the instrumented wheel — common in maturin mixed layouts.

**Cost.** The first run builds the standard library and your crate under TSan: minutes.
`ci` and `stress` use separate work directories (`target/ftcheck-ci`,
`target/ftcheck-stress`), so each builds once; mount a crate cache as in the README's quick
start, or every run downloads your dependencies again.

## Options

| Option | Default | |
|---|---|---|
| `PATH` | `.` | Project root, or the crate directory when it is not at the root |
| `--threads N` | 8 | Threads per test body |
| `--tests PATH` | `tests/` if present, else `.` | Test paths, relative to the project; repeatable |
| `--pytest-arg ARG` | — | One argument passed to pytest; repeatable |
| `--with PACKAGE` / `--extra NAME` | — | Extra packages in the venv / install the wheel with an extra |
| `--no-test-deps` | — | Do not install declared test dependencies |
| `--suppressions FILE` | — | Extra TSan suppressions; repeatable |
| `--python`, `--toolchain` | the image's | The TSan interpreter and matching nightly Rust |
| `--work-dir`, `--target-dir` | `<path>/target/ftcheck-ci` | Where builds and logs go |
| `--sarif`, `--junit`, `--format json` | — | Machine-readable output |
| `--check` | — | Preflight only; builds and runs nothing |

## Attribution: yours, reached from yours, or not yours

Each report is placed by looking at where each conflicting access actually happened:

- **Yours** — on some access, the first frame that is not a TSan interceptor is in one
  of your extension modules. Interceptors live in the interpreter binary, so a race on a
  buffer your C code writes through `snprintf` (top frame: TSan's `vsnprintf`) is yours.
  PyO3, and the standard library under `-Zbuild-std`, are compiled into your module, so
  races there are yours too — you shipped them. These fail the run.
- **Reached from yours** — both accesses happen inside CPython or another library, and
  **every** racing thread passed through your module further down its stack. For
  example, PyO3 methods calling `decimal.Decimal()` from many threads race inside
  CPython's `_decimal` when the threads share one inherited `decimal` context. Real, not your bug:
  reported as `likely`, shown with the frames where it happened, not failing.
  If only **one** side came through your module and the other is plain Python, it is
  **yours when the C-API your code called needs a caller-held lock** on free-threaded
  builds (`PyDict_Next`, the borrowed-reference getters): `PyDict_Next` without a
  critical section, racing a `dict.pop()`, is yours. When the API is
  documented thread-safe (`PyList_GetItemRef`), a race inside it is CPython's: reached,
  not yours.
- **Not yours** — no frame of yours at all. Shown, not failing.

Two refinements: when the *other* access is an allocation into
reused memory (`new_dict`, `PyList_New`, `_PyFreeList_Pop`, …), your code touched an object
after it was freed — **yours**, even though the frames look like CPython's. And reports
from many stacks that share one rule and one primary line are merged into one finding.

**Crashes are findings.** A `SEGV` (or other fatal signal) with your code on the stack,
or with no stack captured at all, fails the run: a process that died while being driven
must never read as clean. If TSan hangs while reporting a crash — it happens — the run is
killed after a short grace period and the crash is still reported.

Suppressions, applied in this order:

1. **ftcheck's own** (`ftcheck/ci/suppressions/rust.supp`) — known deliberate races in
   common Rust dependencies, each justified by that dependency's own source and written
   as `race_top:`, so a real race in your code that merely runs inside the dependency is
   still reported. Today: crossbeam-deque's buffer, as used by rayon's job queue. The
   `clean/clean-rayon` fixture proves both that it is needed (without it: exit 1) and that
   it holds. `FTCHECK_NO_DEFAULT_SUPPRESSIONS=1` turns them off, to audit what they hide.
2. **The image's CPython list** (`/work/tsan_suppressions/cpython.txt`), through
   `$FTCHECK_TSAN_SUPPRESSIONS`.
3. **Yours**, with `--suppressions FILE`.

## Uninstrumented system libraries

After building, ftcheck reads the extension's dynamic dependencies. A library beyond
libc and the toolchain — typically a C dependency found through pkg-config instead of
built from a vendored copy — was not compiled with `-fsanitize=thread`, and races inside
it can be missed. Each one is named in a warning. Many `-sys` crates build the vendored
copy when told to (`libz-sys`, for example, with `LIBZ_SYS_STATIC=1`).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No race detected **on the exercised surface** |
| `1` | A finding: a TSan report in your extension, or a crash |
| `2` | Usage error |
| `3` | Could not run: preflight failed, the build or a dependency install failed, TSan aborted, or **no test executed** — the report prints the failing log's last lines and its path |
| `4` | Your suite failed under the threaded, instrumented run and TSan reported nothing in your extension — a real failure, but not a race ftcheck observed |

A run in which every test was skipped exits `3`, not `0`. Nothing was exercised, so
nothing can be reported clean.

## The gotchas it enforces

Each of these fails **silently** — a run that completes, reports nothing, and is wrong.

| Gotcha | What goes wrong | Preflight check |
|---|---|---|
| CPython's LLVM major ≠ nightly Rust's | Races hide. This hid PyO3's own BorrowFlag race | `llvm_match` |
| `TSAN_OPTIONS=force_seq_cst_atomics=1` | Every atomic becomes SeqCst, masking ordering races | `tsan_options` |
| No `rust-src` | std stays uninstrumented | `rust_src` |
| Interpreter not TSan-built, or not free-threaded | Nothing to observe, or the GIL serialises everything | `tsan_interpreter`, `free_threaded` |
| C deps built by gcc, or not at all | A second TSan runtime, or blind spots | `c_compiler` |
| No `llvm-symbolizer` | Reports cannot be mapped to source | `symbolizer` |
| 3.13t instead of 3.14t | Many CPython-internal reports (a warning, not a refusal) | `python_version` |

### Matching LLVM

The `docker/Dockerfile` pins `RUST_NIGHTLY=nightly-2026-01-15` (LLVM 21.1.8) to match the
base image's clang-21, and **refuses to build** if they disagree. Nightly moved to LLVM 22
between 2026-01-15 and 2026-02-01. When the base image moves to a newer clang, bump the
nightly with it; the build will tell you if you forget.

### The host: ASLR

TSan's memory layout does not fit when the kernel's ASLR entropy is above 28 bits, and
modern distributions and GitHub's runners ship 32. There are two fixes:

- **`--security-opt seccomp=unconfined`** on `docker run`. TSan then disables ASLR for its
  own process and re-executes. Nothing on the host changes. This is what ftcheck's own
  CI and test suite use.
- `sudo sysctl -w vm.mmap_rnd_bits=28` on the host.

No image can fix this for you: it is the host kernel's setting. It is also why Python
packages cannot be installed **during `docker build`** — the build sandbox offers no way
to relax ASLR, so the TSan interpreter cannot start there. The image carries maturin as a
standalone binary for that reason, and `ftcheck ci` installs Python dependencies at run
time.

## In GitHub Actions

This repository is a composite Action. It builds the ftcheck-tsan image from its own
checkout and runs `ci` or `stress` against yours:

```yaml
  free-threading:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: latchwright/ftcheck@<ref>
        id: ftcheck
        with:
          command: stress           # or ci
          args: --seed 1234 --budget 300
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with: { sarif_file: ftcheck.sarif }
```

Inputs: `command` (`ci` | `stress`), `path` (default `.`), `args`, `sarif` (default
`ftcheck.sarif`; empty to skip). Output: `exit-code`. The step fails on any non-zero
exit, so `1` (findings) and `3` (could not run) both fail the job — deliberately.

It does not use `actions/setup-python`: a TSan-instrumented interpreter is not something
setup-python provides, and the image is where the LLVM match is guaranteed. The run step
has been exercised locally with GitHub's environment simulated; it has not yet run on
GitHub.

## What it does not do

- **It does not prove the absence of races.** TSan is dynamic: it sees the interleavings
  that happened, on the code your tests reached.
- **It exercises only what your tests touch.** A method no test calls from a shared
  instance is not examined. `ftcheck stress` ([stress.md](stress.md)) drives the method surface directly
  and reports the coverage it achieved.
- **Prebuilt native libraries are not instrumented.** Anything compiled by your build
  scripts is; a system library or a wheel dependency (numpy, for example) is not, and races
  inside it can be missed. The upstream project publishes `numpy-tsan` and `scipy-tsan`
  images for that case.
- **Linux x86-64 only** for now, because the upstream ThreadSanitizer images are. `lint`
  and `matrix` run wherever the package installs.

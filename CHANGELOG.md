# Changelog

## Unreleased

### Fixed

- **A mutator refilling another library's buffer is a harness race.** A mutator writing
  into a numpy array (`arr[:] = ...`) reaches the memory through numpy's copy loop, not
  CPython's, and was filed as the extension's `certain` race, failing the run. A copy
  primitive called from a library outside the interpreter, with no frame of the extension
  and no free, realloc or resize on the mutator's stack, is now a harness race. Harness
  races are located at the extension's access.
- **A bare source file name is never a file in the crate.** An uninstrumented library's
  debug info can name a file with no directory; resolved against the working directory
  (the crate root, in the image) it became a finding's primary location and merge key.
- **Races outside the extension are located at the plain access**, not at an atomic
  helper from CPython's `pyatomic*.h` on the other side.
- **`ci` runs the tests the project configures.** With no `--tests`, the default is now
  pytest's `testpaths` (then `tests/`, then the project root), and the summary says which
  was chosen. Test requirements come from a `requirements.txt` beside the selected tests:
  `--tests other` no longer installs `tests/requirements.txt`.

### Changed

- Race messages name the threads (`by thread T5 (ftm-refill)`), the text summary labels
  each stack with its thread, and JSON stacks carry a `thread` field.
- A race whose access is in PyO3's own source says so, with the PyO3 version and line.
- **`stress/panic` is located per call, not per message.** The driver records the native
  thread of each panic and Rust prints the same id in its panic line, so two sites that
  panic with the same message are two findings, each naming the callables that panicked
  there with their own counts. When Rust prints no thread id, a finding lists every
  site that shares the message.
- **A panic inside a dependency says so.** The message names the dependency and its
  version (and PyO3's argument conversion when the site is there), and the headline
  counts it as "inside a dependency, reached from your extension". When the log holds a
  Rust backtrace, the first frame in the crate's own code is the location.
- A `stress/panic` message says when mutators were running.

### JSON

- `stress.panics`: every distinct panic message per callable, with a count.
- `stress.panic_threads`: per native thread id, the panicking calls in order, as
  `[callable, message, count]` runs.
- A `stress/panic` finding inside a dependency carries `dependency` (for example
  `"pyo3 0.29.2"`).

## 0.1.0 — 2026-09-25

First release.

### Commands

- **`ftcheck ci`** — builds a Rust/PyO3 extension with ThreadSanitizer (`-Zbuild-std`, C
  dependencies instrumented by the interpreter's own clang) on the upstream
  `ghcr.io/nascheme/cpython-tsan` free-threaded CPython, installs the project's declared
  test dependencies, and runs its own tests with every test body in N threads. Refuses
  (exit 3) wherever a TSan run would be silently wrong: mismatched LLVM,
  `force_seq_cst_atomics`, missing `rust-src`, an uninstrumented or GIL-enabled
  interpreter, the host's ASLR entropy. A suite that fails under threads without a race
  exits 4, apart from findings.
- **`ftcheck stress`** — builds one instance of each type and drives every pair of its
  methods (as the time budget allows) from N threads at once under TSan, with a seed that replays the schedule and a
  coverage report naming everything it could not drive. Factories are declared in one
  line or derived for primitive constructors and enums. Reports crashes, hangs
  (`stress/hang`) and panics that happen only under concurrency (`stress/panic`), not
  just data races.
- **Attribution** — a race is the extension's when its own code made one of the
  accesses, or called a C-API needing a caller-held lock while plain Python raced it;
  races inside CPython that the extension merely reached are shown, not failed.
- **`ftcheck lint`** — a small static pre-flight: FT001 (unsynchronised `static mut` /
  raw-pointer access), FT002 (freelist on PyO3 < 0.23.5), FT003 (locks acquired in a
  non-atomic sequence).
- **`ftcheck matrix`** — free-threaded wheel jobs for a release workflow, each
  interpreter named explicitly.
- **GitHub Action** (`action.yml`) — runs `ci` or `stress` and writes SARIF.

### Evidence

- 18 ground-truth fixtures (8 clean, 7 racy, 3 pending). Every runnable one matches
  under `lint`, `ci` and `stress`, including one whose own tests never share state: `ci`
  reports it clean, `stress` catches the race.
- 18 public PyO3 extensions exercised in two rounds with `stress` and the lint; the false
  alarms and misses those runs exposed are fixed and pinned by fixtures or tests. Results for
  each project will be listed once its maintainers have fixed what was reported. A first-user
  trial on two more projects shaped the docs and the CLI.
- The lint's record on 23 public PyO3 projects, published as found: FT001's 3 findings
  were all false positives (now exempt, with fixtures); FT003's precision went from 13%
  to 67% on rules learned from the rest — measured on the same projects, so not an
  independent figure.

### Packaging

- Wheels on PyPI for CPython 3.11+ (one abi3 wheel per platform) and free-threaded 3.14
  (cp314t) on Linux x86-64 and aarch64, macOS x86-64 and arm64, and Windows x86-64, plus
  an sdist. Only the Python package is published; the Rust crates are internal.

### Known limitations

`ci` and `stress` run on Linux x86-64 only, inside the Docker image; `lint` and `matrix`
run on every platform with a wheel. The historical PyO3 races (#4894, the BorrowFlag race) are not yet
reproduced: they need a 3.13t sanitizer image the upstream registry cannot currently
serve. The Action and `matrix` output have not yet run on GitHub. See
[docs/limitations.md](docs/limitations.md).

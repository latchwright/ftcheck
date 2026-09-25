# Changelog

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

### Known limitations

Linux x86-64 only. The historical PyO3 races (#4894, the BorrowFlag race) are not yet
reproduced: they need a 3.13t sanitizer image the upstream registry cannot currently
serve. The Action and `matrix` output have not yet run on GitHub. See
[docs/limitations.md](docs/limitations.md).

# Limitations

The credibility of a correctness tool is made of what it declines to assert.
This page is maintained as carefully as the rules are.

## ftcheck does not prove the absence of races

The strongest claim it makes is *"no race detected on the exercised surface"*.
Never *"safe"*. A test asserts that the word does not appear in the terminal summary.

## What exists today

| Command | State |
|---|---|
| `ftcheck lint` | **Works**, on every platform with a wheel. FT001 and FT002 at `certain`, FT003 at `likely` |
| `ftcheck ci` | **Works**, Linux x86-64, inside the `docker/` image or an equivalent |
| `ftcheck stress` | **Works**, same environment as `ci`. See [stress.md](stress.md) |
| `ftcheck matrix` | **Works** as a generator; its output has not yet run on GitHub. See [matrix.md](matrix.md) |

`ftcheck ci` and `ftcheck stress` have been run under ThreadSanitizer against every
fixture that can be built, and `stress` against public PyO3 extensions in two rounds and a
first-user trial (see "Runs on public projects" below).

## `ftcheck ci` sees only what your tests reach

It runs your suite with every test body in N threads. A method no test calls on a
shared instance is not examined, and a suite that never shares an object between tests
shares nothing between threads either. `fixtures/racy/stress-unshared-state` is exactly
that case: `ci` reports it clean.

## `ftcheck stress` sees only what it can build and call

It drives one shared instance per type. A type whose constructor needs more than
primitives is not driven until you declare a factory; a method whose arguments cannot be
derived is not driven until you declare them. Both are named in every report, with the
reason — the report says how much of the surface was exercised, and "no race detected"
means no more than that. Derived arguments reach a method's body, not necessarily its
interesting paths. Replaying a seed reproduces the call schedule, not the OS thread
interleaving. See [stress.md](stress.md).

## What `stress` does not report yet

Known gaps, each still open:

- **Wrong results that return normally.** `stress` reports crashes, hangs, panics and
  TSan reports. A call that returns a wrong value under concurrency and raises nothing is
  invisible to it.
- **Exceptions raised only under concurrency**, other than Rust panics, are counted in
  the JSON output (`stress.exceptions`) but not surfaced in the text summary.
- **A panic raised inside a dependency's code** is labelled with the dependency and
  located at its source line. The frame of yours that led there is found only when the
  log holds a Rust backtrace: replay with `RUST_BACKTRACE=1` set. Backtraces are off by
  default because printing one for every panic slows each panicking call about tenfold,
  which changes the schedule under test.
- **Panic sites are told apart by thread id**, which current Rust prints in its panic
  line. With a toolchain that prints none, a panic is located by its message, and when
  two sites share the message the finding lists both without saying which call panicked
  where.
- **A panic on input a mutator made invalid reads as a concurrency panic**, because the
  single-threaded baseline never sees the mutator's transient state. The finding says
  when mutators were running, but does not check whether one caused it. A mutator must
  keep the shared inputs valid at every instant.
- **Some dependency-internal TSan reports still fail runs.** Beyond the suppressed
  crossbeam-deque race, reports inside dependencies' fence-based synchronisation (which
  TSan does not model) and glibc's thread-local teardown can be filed as "in your
  extension" with no frame of yours on either access.

## The lint on public projects

Run over 23 public PyO3 projects on 2026-09-24, the lint reported **3 `certain` findings
and all 3 were false positives** — two on the buffer protocol's caller-owned `view`, one
on a `#[pyclass(unsendable)]` type. Both patterns are now exempt, each backed by a clean
fixture reconstructed from the case. Of the 31 `likely` FT003 findings, 4 were real (13%);
after rules learned from the other 27, it reports 6 with the same 4 real (67%) — see
[rules.md](rules.md). The 67% is measured on the same projects the rules were learned
from, so it is not an independent estimate of precision on new code.
The run also showed that extensions written against raw `pyo3-ffi` rather than PyO3's
macros expose no entry points the lint can see, so the lint is silent on them, whatever
they contain.

## Prebuilt native libraries are not instrumented

C and C++ compiled by your build scripts are instrumented, by the clang that built the
interpreter. A system library, or a native wheel your tests import, is not — and TSan
can miss races inside uninstrumented code. For NumPy and SciPy the upstream project
publishes instrumented images.

## Two fixtures cannot be run under the sanitizer

`racy/ft002-freelist-old-pyo3` (PyO3 #4894) and `pending/pyo3-4948-borrowflag` (the
BorrowFlag race) pin PyO3 0.23, which cannot target Python 3.14, and on 2026-09-24 the
upstream `cpython-tsan:3.13t` image could not be pulled — its manifest index names an
amd64 manifest the registry no longer serves. Both carry `tsan_pending` with that reason
and are reported as expected failures, never skipped. So the historical PyO3 races that
motivated this project have **not** yet been reproduced by it.

## FT001 sees only direct containment

A `#[pyfunction]` that touches a `static mut` or dereferences a raw pointer **in its
own body** is flagged. A `#[pyfunction]` that calls a helper which does the same is
**not**. Whole-program reachability needs call-graph analysis across crate boundaries,
and a rule claiming it without doing the work would be worse than one that states its
limit. A test pins this as intended behaviour so it cannot rot into an accident.

## FT004 and FT005 are unimplemented

Ground truth for both exists in `fixtures/pending/` and is reported as an expected
failure, never silently skipped.

- **FT004** (`OnceLock` initialiser with an observable side effect) needs a judgement
  about what counts as observable that the AST does not carry.
- **FT005** (unsynchronised use of a non-thread-safe C dependency) needs a maintained
  list of C entry points documented as unsafe. Nobody has one.

## FT002 depends on a version it may fail to resolve

FT002 fires only when the resolved PyO3 version is below 0.23.5. If the version cannot
be resolved from `Cargo.lock` or `Cargo.toml`, the rule reports at `likely` rather than
guessing in either direction — so it is hidden by default and available on request.

## The lint is a pre-flight, not the product

It is deliberately small. `rustc` already rejects most of what a naive rule set would
check: `#[pyclass]` must be `Sync`, so a `RefCell` field does not compile, and
`GILProtected` is gated out of free-threaded builds entirely. What survives is the
`unsafe`-mediated and logic-level remainder.

## Runs on public projects

18 public PyO3 extensions were exercised in two rounds in September 2026, with `stress`
and the lint, and two more in a first-user trial. What they found is reported to each
project first; it will be listed here per project once the maintainers have fixed it, with
the report and the fix linked. Until then there is nothing here to cite.

Every one of those runs also changed ftcheck: races inside CPython blamed on the
extension, crashes and hangs lost, panics hidden, a dependency's deliberate race — each
was a false alarm or a miss on real code. Most are now fixed and pinned by a fixture or a
test; the ones still open are listed under "What `stress` does not report yet" above.
The confirmed-to-reported ratio for the dynamic tools will be published here as more
projects are run, including when it is unflattering.

## Three corrections to the specification this was built from

Recorded because a tool that quietly inherits a wrong premise is how wrong premises
survive.

1. **FT002 was specified as unconditional and `certain`.** PyO3 0.23.5 (2025-02-22)
   fixed the underlying bug in PR #4902, and the freelist has been mutex-guarded since.
   Firing unconditionally would be a false positive on every current project.
2. **PyO3 #4904 was described as a "pyclass type-object init" race.** It is a bundle of
   ThreadSanitizer reports. Its type-object trace was a CPython bug, fixed upstream as
   python/cpython#130421. The race PyO3 fixed from that bundle was in `BorrowFlag`,
   via PR #4948, released in 0.24.0.
3. **The TSan-instrumented free-threaded CPython image was described as unpackaged, at
   a build cost of 45–60 minutes.** Both are false. `ghcr.io/nascheme/cpython-tsan`
   has shipped one for over a year, both the py-free-threading guide and the PyO3 guide
   recommend it, NumPy's CI consumes it, and the measured build takes 6–10 minutes.
   `ftcheck ci` consumes that image rather than duplicating it.

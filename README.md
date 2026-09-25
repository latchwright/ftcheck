# ftcheck

> **Pre-release.** Read [docs/limitations.md](docs/limitations.md) before trusting any output.

`ftcheck` looks for free-threading bugs in Rust/PyO3 extensions: data races, crashes,
hangs and panics that appear only when Python threads really run in parallel.

A Rust extension that was correct under the GIL can be silently wrong without it, because
the GIL provided mutual exclusion the author never had to think about. The failure mode is
data corruption and rare crashes, not a compile error.

## Two things this does not claim

Stated up front, because they are the claims a tool in this space is tempted to make.

- **It does not bring free-threading to GitHub Actions.** `actions/setup-python` has
  installed `3.13t` and `3.14t` since PR #973.
- **It does not generate a concurrency harness automatically.** Constructor synthesis for
  non-primitive `#[pyclass]` types is not viable: `stress` derives constructors that take
  only primitives, and any other type needs a declared factory.

It also **does not distribute a ThreadSanitizer-instrumented CPython image**, because one
already exists: [`nascheme/cpython_sanity`](https://github.com/nascheme/cpython_sanity)
publishes `ghcr.io/nascheme/cpython-tsan`, both the py-free-threading guide and the PyO3
guide recommend it, and NumPy's CI already uses it. `ftcheck ci` consumes that image and
adds only the Rust layer.

## Quick start

Everything runs from one Docker image: the upstream ThreadSanitizer free-threaded CPython,
a matching nightly Rust, and ftcheck. Build it once, from this checkout:

```console
$ docker build -f docker/Dockerfile -t ftcheck-tsan .
```

Then, from your extension's directory, define the one prefix every command uses:

```console
$ mkdir -p ~/.cache/ftcheck-cargo
$ FTCHECK='docker run --rm --security-opt seccomp=unconfined --user '"$(id -u):$(id -g)"' -e HOME=/tmp -v '"$PWD"':/src -v '"$HOME"'/.cache/ftcheck-cargo:/opt/cargo/registry ftcheck-tsan ftcheck'
$ $FTCHECK lint /src      # seconds: a static pre-flight
$ $FTCHECK ci /src        # minutes: your tests, every test body in 8 threads, under TSan
$ $FTCHECK stress /src    # minutes: one shared instance per type, driven from 8 threads
```

What each part is for:

- `--security-opt seccomp=unconfined` — ThreadSanitizer cannot start under the ASLR
  entropy modern kernels use; this lets it disable ASLR for its own process. Needed by
  `ci` and `stress`; harmless for the rest. See [docs/ci.md](docs/ci.md#the-host-aslr).
- `--user … -e HOME=/tmp` — build output lands in your project owned by you, not root.
- `-v ~/.cache/ftcheck-cargo:/opt/cargo/registry` — keeps downloaded crates between runs;
  without it every run downloads them again.
- If your crate is not at the repository root, pass its directory (`/src/bindings/python`)
  and still mount the whole repository, so path dependencies resolve.

`lint` and `matrix` also run without Docker: `pip install /path/to/ftcheck-checkout`
(needs Rust and Python ≥ 3.11), then `ftcheck lint .`.

## What works today

### `ftcheck ci` — the ThreadSanitizer pipeline

Builds your extension with `-Zsanitizer=thread` and `-Zbuild-std`, installs it on a
TSan-instrumented free-threaded CPython, and runs **your own test suite with every test
body in N threads at once**, so a module-level instance your tests touch is shared
across threads — the situation the GIL used to make safe.

```console
$ $FTCHECK ci /src
built ft001_static_mut.cpython-314t-x86_64-linux-gnu.so with ThreadSanitizer
ran 1 test x 8 threads (pytest exit 0; 0 failed, 0 errors)

1 finding:

  tsan/data-race  src/lib.rs:14:9  [certain]  (reported 2x)
        ThreadSanitizer: data race. Write of size 8 by thread T7 in `bump`;
        Previous write of size 8 by thread T8 in `bump`
```

Before it builds anything it checks every condition under which a TSan run is
**silently** wrong — mismatched LLVM between Rust and CPython, `force_seq_cst_atomics`,
missing `rust-src`, a GIL-enabled or uninstrumented interpreter — and refuses with exit
`3` rather than reporting a clean run. See [docs/ci.md](docs/ci.md) for the full command,
GitHub Actions usage, and the host ASLR requirement.

### `ftcheck stress` — one shared instance, many threads

`ci` finds a race only if your tests happen to share an object between threads, and
most suites build a fresh one per test. `stress` builds **one** instance of each type and
drives every pair of its methods from N threads at once — as many pairs as the time
budget allows, and the report says how many that was — under ThreadSanitizer, with a
seed that replays the schedule:

```console
$ $FTCHECK stress /src
seed 1234  threads 8  iterations 200  budget 120s
  replay: ftcheck stress --replay 1234 --threads 8

coverage: 1 of 1 callables driven

  ft001_raw_pointer.ft001_raw_pointer.Buffer  [derived: Buffer(1)]  1/1 pairs
    driven      ft001_raw_pointer.ft001_raw_pointer.Buffer.bump  (3200 calls; raised IndexError x1099)

1 finding:

  tsan/data-race  src/lib.rs:35:13  [certain]  (reported 2x)
```

No test was written for that. Constructors that need more than primitives take a
one-line factory in `ftcheck.toml`; every callable that could not be driven is named, with
the reason. `fixtures/racy/stress-unshared-state` proves the gap: its own tests never
share an instance, and the ground truth asserts **`ci` exits 0 and `stress` exits 1** on
it. See [docs/stress.md](docs/stress.md).

### `ftcheck lint` — a fast pre-flight

```console
$ ftcheck lint path/to/your/crate
scanned 1 file, 1 Python entry point, PyO3 0.29.2

1 finding:

  FT001  src/lib.rs:14:9  [certain]
        `bump` reaches the `static mut` `COUNTER` with no synchronisation
```

The lint is **not the product**. It is deliberately small, because `rustc` already
rejects most of what a naive rule set would check. See [docs/rules.md](docs/rules.md).

`ftcheck matrix` prints free-threaded wheel jobs for a release workflow, with every
interpreter named explicitly. Check what your maturin version's `generate-ci` already
emits first. See [docs/matrix.md](docs/matrix.md).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | No confirmed finding **on the exercised surface** |
| `1` | Confirmed finding: a TSan report in your extension, a crash, a hang, or a panic only under concurrency |
| `2` | Usage or configuration error |
| `3` | Could not run — toolchain or interpreter failure, nothing exercised, or coverage below `--min-coverage` |
| `4` | `ci` only: your test suite failed under the threaded, instrumented run, and TSan reported no race — often a test that is not thread-safe |

`3` is the one that matters. ftcheck never claims code is safe, only that nothing was
found on the surface it actually exercised — so a run that never happened must not be
reportable as a clean one.

## What it refuses to assert

- **It does not prove the absence of races.** The report reads *"no race detected on the
  exercised surface"*, never *"safe"*. A test asserts that word never appears.
- **It reports its own coverage** — files scanned, entry points seen, and every file it
  could not parse, named rather than skipped; tests run and threads used. A sanitizer
  run that executed no test exits `3`, not `0`.
- **Races inside CPython are reported separately** and do not fail your run — including
  those your extension merely reached, which are labelled as such rather than blamed on
  it.
- **It does not stop at data races.** A crash, a hang, or a Rust panic that happens only
  under concurrency is a finding too; each is a real failure users would hit, and none of
  them is a TSan data-race report. What `stress` cannot yet see — wrong results that
  return normally, among others — is listed in
  [docs/limitations.md](docs/limitations.md#what-stress-does-not-report-yet).
- **`likely` findings are hidden by default** and do not affect the exit code unless you ask for them (`lint --confidence likely`).
- **It does not rate severity or exploitability.**
- **It does not replace `pytest-run-parallel`.** That tool runs your existing tests in
  parallel; it cannot discover issues arising from multithreaded use of data structures
  defined by the library under test, which is what `ftcheck stress` is for.
- **The lint's record on public code is published, unflattering or not.** Its first run
  over 23 public PyO3 projects: 3 `certain` findings, 0 confirmed; FT003 at 13% precision.
  Both were fixed from that evidence. FT003 now measures 67%, but on the same projects the
  fixes were learned from, so that is not an independent figure. See
  [docs/limitations.md](docs/limitations.md).

## Ground truth

Every rule is backed by fixtures in [`fixtures/`](fixtures/), each carrying an
`expected.toml` that states what a correct tool must say about it — both what the lint
must report and whether ThreadSanitizer must see a race. **A finding on a clean fixture
fails the suite exactly as hard as a miss on a racy one**, in both halves.

The most useful pair is `racy/ft002-freelist-old-pyo3` and
`clean/clean-freelist-current-pyo3`: the same source apart from the module name, pinned
to different PyO3 versions, because whether that code is a bug depends on the framework
version and not on the code.

## Development

```console
cargo test --workspace     # rust unit tests
pytest tests/              # contract, CLI and ground-truth suites
./scripts/check-private.sh # publish guards

docker build -f docker/Dockerfile -t ftcheck-tsan .
FTCHECK_TSAN=1 pytest tests/test_tsan_ground_truth.py   # every fixture under TSan
```

## Licence

MIT OR Apache-2.0.

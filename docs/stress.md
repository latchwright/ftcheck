# `ftcheck stress` — one shared instance, many threads

`ftcheck ci` runs your tests with every test body in N threads. It finds a race only if
your tests share an object between threads — and most suites build a fresh object in
every test, so N threads get N objects and share nothing. `pytest-run-parallel`
documents the same limit: it cannot discover issues from multithreaded use of data
structures defined by the library under test.

`ftcheck stress` closes that gap. It builds **one** instance of each of your types and
drives its methods from N threads at once, every pair of methods overlapping as far as
the time budget allows, under ThreadSanitizer, with a seed that reproduces the schedule.

```console
$ docker run --rm --security-opt seccomp=unconfined \
      --user "$(id -u):$(id -g)" -e HOME=/tmp \
      -v "$PWD":/src ftcheck-tsan ftcheck stress /src
seed 1234  threads 8  iterations 200  budget 120s
  replay: ftcheck stress --replay 1234 --threads 8

coverage: 1 of 1 callables driven

  ft001_raw_pointer.ft001_raw_pointer.Buffer  [derived: Buffer(1)]  1/1 pairs
    driven      ft001_raw_pointer.ft001_raw_pointer.Buffer.bump  (3200 calls; raised IndexError x1099)

1 finding:

  tsan/data-race  src/lib.rs:35:13  [certain]  (reported 2x)
```

No test was written for that. The environment, image and host requirements are the same
as for `ci` — see [ci.md](ci.md).

## The proof, in this repository

`fixtures/racy/stress-unshared-state` is a hand-`Sync` type that races, with an ordinary
test suite that builds a fresh instance per test. The sanitizer ground truth asserts
both halves of the claim on it: **`ci` exits 0, `stress` exits 1.**

## Factories

What `stress` drives, per type, comes from — in this order:

1. **A declared factory**, one Python expression per type:

   ```toml
   # ftcheck.toml            (or [tool.ftcheck.stress.factories] in pyproject.toml)
   [stress.factories]
   "mypkg.Tally" = "mypkg.Tally(mypkg.Settings(4))"
   ```

   Keys may use the public name (`mypkg.Tally`) or the full native-module path
   (`mypkg.mypkg.Tally`). The native module and its top-level package are importable by
   name inside the expression.

2. **A factories file**, for anything an expression cannot say:

   ```toml
   [stress]
   factories_file = "tests/ftcheck_factories.py"
   ```

   ```python
   # tests/ftcheck_factories.py — plain Python, imports nothing from ftcheck
   import tempfile, mypkg

   def make_store():
       return mypkg.Store(tempfile.mkdtemp())

   FACTORIES = {"mypkg.Store": make_store}
   ARGS = {"mypkg.Store.put": [("key", b"value")]}
   ```

3. **Derived** — for constructors with no required parameters, or only parameters a
   small pool of primitives satisfies (int, str, bytes, float, bool, None, list, dict),
   tried in an order guided by parameter names; a candidate counts only if the
   constructor **returns**. A PyO3 enum is derived from its first variant. `stress` does
   not guess further than that: constructors that need a path, a particular string, or
   another of your types need a declared factory.

4. Otherwise the type is **not driven**, and every one of its methods is listed with the
   reason.

### Method arguments

```toml
[stress.args]
"mypkg.Tally.record" = ["0", "3"]                 # each string is one call's arguments
"mypkg.parse" = ["b'{}', cache_mode='all'"]       # keyword arguments too
```

In a factories file, `ARGS` entries may be a tuple of positional arguments, a dict of
keyword arguments, or an `(args, kwargs)` pair.

Undeclared arguments are derived from the same pool on a scratch instance (never the
shared one), in a scratch working directory. A call counts as supplied when it does not
raise `TypeError` — PyO3's argument-extraction failure. Up to three distinct argument
tuples are kept per method.

**Arguments are shared by every thread.** An argument expression is evaluated once, and
the same object is passed to every call on every thread. That is usually the point — a
shared dict handed to a serializer from eight threads — but an *output* buffer in the
arguments (`encode_into(..., buf)`) makes every thread write the same buffer, a race in
your harness rather than in the extension. Give such calls a fresh buffer from a factory
function instead.

A **private method** (`_fast_path`) is part of the surface when you declare arguments
for it — that is how to reach an `unsafe` block kept behind an underscore.

### Mutators — a Python writer alongside the calls

Some races need a plain-Python thread changing an object *while* the extension reads it:
a dict mutated during serialization, a list shrinking mid-iteration. The driven calls
alone never provide that writer. Declare **mutators**: callables run in a loop on their own
threads for the whole of every phase.

```python
# factories file
import mypkg
SHARED = {"a": [1, 2, 3], "b": "x" * 100}

def churn():
    SHARED["tmp"] = [0] * 50
    SHARED.pop("tmp", None)

ARGS = {"mypkg.dumps": [(SHARED,)]}
MUTATORS = {"churn": churn}
```

or in TOML, `[stress.mutators]` mapping a name to an expression that evaluates to a
callable. Mutator calls and exceptions are counted in the result (`mutator:<name>`). On the
public projects they were tried on, short mutator configs like this one re-created every
known container-mutation crash. Some of those configs were written knowing where the bug
was, so that shows mutators reach this class of bug, not that they find it unaided.

A mutator must keep the shared inputs **valid at every instant**. The baseline never sees
a mutator's transient state, so a call that panics on input the mutator made invalid is
reported as a panic under concurrency.

A mutator that only **rewrites a buffer's contents** in place (a slice assignment into a
`bytearray` the extension is reading, or a numpy array refilled with `arr[:] = ...`) races
by the buffer protocol's own contract; it is reported as a **harness** race — shown, not
failing, and located at the extension's read. A mutator whose resize or free makes
the extension touch freed memory is a finding like any other, and so is a copy inside
one of CPython's own containers (`list.insert`): those run under the container's critical
section, which the extension must take too. Mutator threads are named
`ftm-<name>` (cut to Linux's 15 characters), so TSan reports say which mutator it was.

### Skipping

```toml
[stress.skip]
"mypkg.Store.close" = "invalidates the instance"
"mypkg.serve" = "blocks forever"
```

Anything that blocks, ends the instance's life, or touches the world outside the
process belongs here. A skip is reported with its reason, like any other gap.

## What is driven

For each native module in the built wheel — and each PyO3 submodule inside it — its
public classes and functions. For each class, including members inherited from a base
class that is not exported: public methods, getters and — as their own callable, writing
back the value just read — setters, static and class methods, and `__len__`, `__getitem__`,
`__contains__`, `__iter__`, `__repr__` when defined. Module functions are driven as one
group, since they share the module's global state — which is where `static mut` lives.

For each type, with one shared instance:

1. **Pairs** — every unordered pair of callables, including each with itself, in an
   order the seed shuffles. Each group gets a fair share of the remaining budget, so one
   slow type cannot starve the rest. Half the threads loop one callable, half the other,
   released together by a barrier.
2. **Mix** — every thread draws callables and arguments from its own seeded RNG.

Every exception is caught and counted — including PyO3's `PanicException`, which is a
`BaseException`. `Already borrowed` from PyO3's runtime borrow checker is the correct
response to contention on a `&mut self` method, not a finding. Each worker thread runs in
an empty `contextvars` context: on 3.14t a new thread otherwise inherits its parent's
context values, and races on shared objects such as a `decimal` context would appear.

A callable whose **every** call raised is marked `RAISED ALL` and is **not** counted as
driven: usually only its argument checks ran, not its body — declare valid arguments for
it. (If raising is the method's normal result, as `validate` raising a validation error,
that is fine; the count stays strict on purpose.) A callable the run never reached — it
ended first — is marked `not reached`. Coverage counts callables, not inputs: a callable
driven only with derived primitive arguments counts as driven, and may still have its
interesting paths unexercised.

The **single-threaded baseline** calls every argument tuple three times in sequence on
one instance before driving, so a panic on a repeated call (a duplicate insert, a lock
poisoned by an earlier panic) is recorded as single-threaded behaviour, not reported as a
concurrency finding.

**Thread-confined types** — `#[pyclass(unsendable)]`, which PyO3 refuses to touch from
any thread but the creator — are detected and reported as confined. They cannot race
across Python threads, so they are neither driven nor counted as a coverage hole.
`--budget` bounds the driving time; pairs not reached are reported.

**Panics under contention are findings.** A Rust panic (`PanicException`) that a
callable raises under concurrency but never in its baseline is
reported as `stress/panic`, with the count, the first panic message and — from Rust's
own panic output — the source line that panicked. Callables panicking at the same line are
one finding. The line is matched by panic message, so two sites panicking with the same
message can be filed under one of them (see Limits). It is not a data
race and TSan may see nothing, but it is contention the code does not handle — and since
`PanicException` is a `BaseException`, callers' `except Exception` will not catch it.
A panic the baseline also raised is how the method treats those arguments, and
is left alone.

**Crashes are findings.** If the process dies — a segfault TSan reports (`tsan/segv`), or
a `Fatal Python error` / other fatal signal it does not (`stress/crash`) — the run fails
with the step that was running, even when no race was reported.

**Hangs are findings.** If no call finishes for five minutes during the drive phase, the
run is stopped and reported as `stress/hang` — a possible deadlock — naming the pair of
callables and the seed. Progress is counted per call, so a call that is merely slow (and
gets slower as shared state grows) is not mistaken for a deadlock, and the budget is
checked on every call. TSan does not detect deadlocks between a lock and a
thread pool; this does. A callable that blocks by design belongs in `[stress.skip]`.

## Reproducing a run

Every run prints its seed. `ftcheck stress --replay SEED` reruns the same schedule: the
same calls, arguments and yield points on every thread. Each type's schedule is derived
from the seed and the type's own name, so `--replay SEED --only NAME` gives that type
exactly the schedule it had in the full run. It cannot reproduce the OS
scheduler's exact interleaving — nothing can. ThreadSanitizer detects races by
happens-before rather than by catching a corrupted value, so replaying the schedule
re-exercises the same unordered accesses and re-detects the race in practice. `--only
NAME` narrows a replay to one type.

## Coverage is reported, not implied

Every callable on the surface is **driven** or **not driven, because …**:

```text
coverage: 3 of 5 callables driven

  mypkg.Store  [no factory]  0/0 pairs
    NOT driven  mypkg.Store.get
                no factory: constructor parameters ['path'] could not be satisfied
                from primitives (OSError: …)
```

A run that drove nothing exits `3`, not `0`. If a run ends early — a crash, a hang, a kill —
the report still shows what was driven before it ended, and marks the group that was in
progress.

## Exit codes

`0` no race detected **on the exercised surface** (the summary says how much of the
surface that was) · `1` a finding: a TSan report in your extension, a crash, a
`stress/hang` or a `stress/panic` · `2` usage error, including an `--only` name that
matches nothing (the message lists the names that exist) · `3` could not run — including
"drove nothing", and coverage below `--min-coverage`.

## Options

| Option | Default | |
|---|---|---|
| `PATH` | `.` | Project root, or the crate directory when it is not at the root |
| `--seed N` / `--replay N` | random, printed | Schedule seed; `--replay` reruns one |
| `--threads N` | 8 | Threads driving each shared instance |
| `--iterations N` | 200 | Calls per thread per phase |
| `--budget SECONDS` | 120 | Wall-clock budget for driving, shared fairly between types |
| `--only NAME` | all | One type or module, by public or full name; repeatable |
| `--module NAME` | from config, else every native module in the wheel | Native module to drive |
| `--min-coverage PERCENT` | 0 | Exit 3 unless at least this share of the surface was driven |
| `--with PACKAGE` | — | Extra package in the venv (e.g. `numpy`); repeatable |
| `--suppressions FILE` | — | Extra TSan suppressions; repeatable |
| `--work-dir`, `--target-dir` | `<path>/target/ftcheck-stress` | Where builds and logs go |
| `--sarif`, `--junit`, `--format json` | — | Machine-readable output |
| `--check` | — | Preflight only |

Configuration is read from `[tool.ftcheck.stress]` in the project's `pyproject.toml`, or
from `[stress]` in an `ftcheck.toml` beside it — at `PATH`. A `factories_file` is
relative to that directory. Every run prints the complete command that replays it.

## Limits

- **Importing is required** to enumerate the surface, so your module's import side
  effects run.
- **Derived arguments are shallow.** A method whose interesting paths need structured
  input is reached, but not deeply, until you declare arguments.
- **One instance per type.** Races between two instances sharing hidden global state are
  reached only through module functions or your own factories.
- **Replay fixes the schedule, not the interleaving** (above).
- **Wrong results are not checked.** A call that returns a wrong value under concurrency
  without raising is invisible; exceptions other than panics that occur only under
  concurrency are counted in the JSON output but not surfaced in the summary.
- **Panic attribution is coarse.** A panic inside a dependency is located at the
  dependency's source line, not at your frame that led there; panic sites are matched by
  message text, so one site can hide another with the same message.
- **Mutators must keep inputs valid** (above), or their transient state reads as a
  concurrency panic.
- See [limitations.md](limitations.md#what-stress-does-not-report-yet) for the full list.

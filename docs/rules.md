# Rules

Every rule names what it detects, what it deliberately does not, and the fixture that
proves it. `certain` findings are shown by default and fail CI; `likely` findings are
hidden unless `--confidence likely` is passed, and never affect the exit code otherwise.

Retired or unimplemented ids are never reused.

---

## FT001 — unsynchronised shared mutable state reachable from Python

**Confidence:** `certain`

Fires when a `#[pyfunction]` body, or a method of a `#[pymethods]` impl, either:

- accesses a `static mut` declared in the same file, or
- dereferences a pointer inside an `unsafe` block.

The second is precise without type information: a safe dereference of a reference or a
`Box` needs no `unsafe` block, so a dereference that has one is a raw pointer.

**Does not detect:** anything reached indirectly. See [limitations](limitations.md).
Also deliberately silent on reading a `*const c_char` returned by FFI — flagging every
FFI string read would drown the rule, and `fixtures/pending/ft005-unsync-c-dep` asserts
that silence.

**Exempt, because nothing is shared** — both found as false positives on public projects:

- Methods of a `#[pyclass(unsendable)]` type. PyO3 checks on every access that such an
  object is used only from its creating thread, and raises otherwise.
- Dereferences of the pointer parameters of `__getbuffer__` and `__releasebuffer__`.
  CPython passes a `Py_buffer` owned by the requester, one per request. Any *other*
  dereference in those methods is still flagged.

**Fixtures:** `racy/ft001-static-mut`, `racy/ft001-raw-pointer`,
`racy/stress-unshared-state`, `clean/clean-atomics`, `clean/clean-unsendable`,
`clean/clean-buffer-protocol`

---

## FT002 — `#[pyclass(freelist = N)]` on a PyO3 version where it is unsound

**Confidence:** `certain` below PyO3 0.23.5 · `likely` when the version is unresolvable ·
**silent** at 0.23.5 and above

PyO3 #4894 made freelist pyclasses thread-unsafe on free-threaded builds. **PR #4902
fixed it, released in 0.23.5 on 2025-02-22.** Since then the freelist lives in a
`PyOnceLock<Mutex<PyObjectFreeList>>`, locked on every allocation and every free.

So the rule is version-gated. Flagging `freelist` on a current PyO3 is a false positive,
and the specification this tool was built from asked for exactly that — see
[limitations](limitations.md).

**Not reported, on purpose:** on PyO3 ≥ 0.23.5, `freelist` remains a *performance*
concern. The mutex added by #4902 costs more than the freelist saves, on **all** builds
including GIL builds, and PyO3 #6133 is open about that cost. ftcheck says
nothing about it: it is a free-threading correctness tool, and rating performance is
outside what it claims.

**Fixtures:** `racy/ft002-freelist-old-pyo3` and `clean/clean-freelist-current-pyo3` —
byte-identical source, differing only in the pinned PyO3 version. That pair is what
stops this rule regressing to its unconditional form.

---

## FT003 — locks acquired in a non-atomic sequence

**Confidence:** `likely` — hidden by default

Fires when one entry point acquires locks at two or more points **while holding none
across them** — including the same lock twice, where state read under the first
acquisition is relied on or written back under the second (a lost update).

**This is not a data race.** Every access is synchronised, so ThreadSanitizer sees
nothing and never will. That is why its fixtures set `race = false` while expecting the
rule.

**Not a sequence, and so not flagged** — each learned from a false positive on public code:

- A lock taken while an earlier guard is still held (`let a = x.lock()?; let b =
  y.lock()?;`) — nested, one critical section.
- `lock`/`read`/`write` calls with arguments (`ptr.write(v)`, `reader.read(buf)`).
- Locks inside `async` blocks, which run later, elsewhere.
- Locks inside an `if` branch that always returns: the path that continues never took
  them.

It stays `likely` because a sequence can still be correct — the locks may guard unrelated
state. The AST cannot tell, so the tool does not insist.

**Does not detect:** two locks within a single expression; lock-order inversion (AB/BA),
which is a different hazard.

**On public code:** 31 reports across 23 projects at first, 4 real (13%). After the rules
above: 6 reports, the same 4 real (67%) — measured on the same projects the rules were
learned from, so not an independent figure. Every real one had the shape
`racy/ft003-snapshot-writeback` pins: a copy taken under one acquisition, work done with
the lock released, and the stale copy written back under another.

**Fixtures:** `racy/ft003-lock-pair`, `racy/ft003-snapshot-writeback`, `clean/clean-mutex`,
`clean/clean-nested-guards`

---

## FT004 — `OnceCell`/`LazyLock` initialiser with an observable side effect

**Status: unimplemented.** Ground truth waits in
`fixtures/pending/ft004-oncelock-side-effect`.

`get_or_init` guarantees one stored value, not one closure execution. Racing threads may
both run the closure; only one result is kept. Anything else the closure does can happen
twice. Deciding what counts as *observable* needs judgement the AST does not carry.

---

## FT005 — unsynchronised access to a C dependency that is not thread-safe

**Status: unimplemented.** Ground truth waits in `fixtures/pending/ft005-unsync-c-dep`.

Needs a maintained list of C entry points documented as not thread-safe. Nobody has one,
and inventing an unmaintained one would produce findings nobody can act on.

---

## Rules that were cut before implementation

The specification's first draft linted `RefCell` and `Rc` fields on `#[pyclass]`,
non-`Sync` statics, and `GILProtected`. All three were cut, because **`rustc` already
rejects them**: `#[pyclass]` requires `Sync`, and `GILProtected` is gated behind
`#[cfg(not(Py_GIL_DISABLED))]`. A lint that reports what the compiler already refuses to
build is noise.

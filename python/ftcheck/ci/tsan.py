# SPDX-License-Identifier: MIT OR Apache-2.0
"""ThreadSanitizer report parsing, attribution and deduplication.

A TSan report is text, and the same race arrives many times: once per
interleaving that hit it, in either order, from any pair of threads. This module
turns that text into findings a person can act on.

**Attribution has three outcomes, decided per access stack.**

- **Yours**: on some conflicting access, the first frame that is not a TSan
  interceptor is in one of your extension modules. Interceptors live in the
  interpreter binary (TSan's runtime is linked into it), so a race on a buffer
  written by `snprintf` from your C code — top frame `vsnprintf`, in the
  interpreter — is still yours. PyO3 and, under `-Zbuild-std`, the standard
  library are compiled into your module, so races there are yours too.
- **Reached from yours**: both accesses happen inside CPython (or another
  library), and on **every** side it is CPython code your module called
  directly — no Python code in between. For example, two threads calling
  `decimal.Decimal()` through a PyO3 method race inside CPython's `_decimal`
  when 3.14t threads inherit the parent's context. Real, but not the
  extension's bug — shown, not failing.
  When only one side is your module's direct call and the other is Python
  code (even a callback your module invoked), it is yours: your code used a
  C-API that requires the caller to hold a lock (`PyDict_Next` without a
  critical section).
- **Not yours**: no frame of yours at all. Kept separately and shown, never
  dropped: hiding it would claim a surface was quiet when it was not.

**A crash is always a finding.** TSan's `ERROR: ThreadSanitizer: SEGV` report
counts when any frame of yours is on the crashing stack — a crash under your
call — and when no stack was captured at all, since a process that died while
being driven must never read as clean.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

__all__ = [
    "Frame",
    "Report",
    "attribute",
    "fatal_errors",
    "parse",
    "to_findings",
]

_SEPARATOR = "=================="
_WARNING = re.compile(r"^WARNING: ThreadSanitizer: (?P<kind>.+?)(?: \(pid=\d+\))?\s*$")
_ERROR = re.compile(
    r"^==\d+==ERROR: ThreadSanitizer: (?P<kind>[A-Za-z-]+?)(?: on unknown address.*| .*)?\s*$"
)
# The interpreter executable (TSan's runtime, and so its interceptors, are
# linked into it) and the runtime's own library.
_INTERCEPTOR_MODULE = re.compile(r"^(python[\d.]*t?|libtsan\.so.*)$")
_CRASH_KINDS = ("SEGV", "BUS", "FPE", "ILL", "ABRT", "stack-overflow", "deadly")
_THREAD_NAME = re.compile(r"^Thread (?P<tid>T\d+) '(?P<name>[^']*)'")
_BY_THREAD = re.compile(r"by thread (?P<tid>T\d+)")
# ftcheck's mutator threads (names are kept under the kernel's 15 characters).
_MUTATOR_PREFIX = ("ftm-", "ftcheck-mutator")  # the latter: logs from before the rename
# CPython routines that only rewrite a buffer's contents in place — no resize,
# no free. A mutator doing this to an exported buffer races by the buffer
# protocol's own contract: the harness's race, not the extension's bug.
_CONTENT_WRITERS = (
    "bytearray_setslice_linear", "bytearray_setslice", "bytearray_ass_subscript",
    "copy_base", "copy_single", "memory_ass_sub", "bytearray_setitem",
)  # fmt: skip
# TSan's interceptors for the copy primitives, as the symbolizer names them.
_COPY_PRIMITIVES = frozenset(
    p for name in ("memmove", "memcpy", "memset") for p in (name, "__tsan_" + name)
)
# A frame that frees, reallocates or resizes: not a rewrite in place.
_RELEASES = re.compile(r"free|realloc|resize|dealloc", re.IGNORECASE)
_INTERPRETER_LIBRARY = re.compile(r"^libpython")
# The other access being an allocation means the memory was freed and reused:
# the extension touched an object that no longer exists.
_ALLOCATORS = (
    "new_reference", "_PyFreeList_Pop", "new_dict", "dict_new_presized", "PyList_New",
    "PyUnicode_New", "PyTuple_New", "PyBytes_FromStringAndSize", "_PyObject_GC_New",
    "_PyObject_New", "PyObject_Malloc", "PyMem_Malloc", "_PyType_AllocNoTrack",
    "PyType_GenericAlloc",
)  # fmt: skip
_FRAME = re.compile(r"^\s+#(?P<index>\d+) (?P<rest>.*)$")
_MODULE = re.compile(r"\((?P<module>[^()\s]+)\+0x[0-9a-f]+\)\s*$")
_BUILD_ID = re.compile(r"\s*\(BuildId: [0-9a-f]+\)\s*$")
_ADDRESS = re.compile(r" at 0x[0-9a-f]+")
_CODEGEN_UNIT = re.compile(r"^[^/]*-cgu\.\d+$")
# A source file of PyO3 itself, as Cargo unpacks it: `.../pyo3-ffi-0.26.0/src/...`.
_PYO3_SOURCE = re.compile(r"/(?P<crate>pyo3(?:-ffi)?)-(?P<version>\d+\.\d+\.\d+[^/]*)/(?P<rest>.+)$")
_LOCATION = re.compile(r"^(?P<file>.+?)(?::(?P<line>\d+))?(?::(?P<column>\d+))?$")

# Sections of a report that describe context rather than a conflicting access.
_CONTEXT_PREFIXES = ("Thread T", "Location is", "Mutex M")

# Lines TSan prints when it could not run at all. Seeing one means nothing was
# exercised, which must surface as "could not run", never as "clean".
_FATAL = ("FATAL: ThreadSanitizer", "ThreadSanitizer: CHECK failed")

# How many frames of each stack a finding keeps. Beyond this the frames are
# interpreter plumbing (the eval loop, thread bootstrap) and add only noise.
_MAX_FRAMES = 12


@dataclass(frozen=True)
class Frame:
    symbol: str
    module: str | None
    file: str | None = None
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class Section:
    header: str
    frames: tuple[Frame, ...]

    @property
    def is_access(self) -> bool:
        return not self.header.startswith(_CONTEXT_PREFIXES) and " created at" not in self.header


@dataclass
class Report:
    kind: str
    sections: list[Section] = field(default_factory=list)
    summary: str = ""

    @property
    def access_stacks(self) -> list[Section]:
        return [s for s in self.sections if s.is_access and s.frames]

    @property
    def is_crash(self) -> bool:
        return self.kind.startswith(_CRASH_KINDS)

    def thread_names(self) -> dict[str, str]:
        """`T5` -> `ftm-churn`, from the "Thread T5 'name' ... created" sections."""
        names = {}
        for section in self.sections:
            m = _THREAD_NAME.match(section.header)
            if m:
                names[m.group("tid")] = m.group("name")
        return names

    def access_thread(self, section: Section) -> str | None:
        m = _BY_THREAD.search(section.header)
        return m.group("tid") if m else None


def _parse_frame(rest: str) -> Frame:
    rest = _BUILD_ID.sub("", rest)
    module = None
    match = _MODULE.search(rest)
    if match:
        module = os.path.basename(match.group("module"))
        rest = rest[: match.start()].rstrip()

    # What remains is "<symbol> <location>". Symbols may contain spaces
    # (`<T as Trait>::f`); source paths do not, so the location is the last token.
    symbol, _, location = rest.rpartition(" ")
    if not symbol:
        symbol, location = location, "<null>"

    file = line = column = None
    if location and location != "<null>":
        loc = _LOCATION.match(location)
        # Without line tables the symbolizer names a codegen unit
        # (`crate.1a2b3c-cgu.0`), not a file; treating it as a relative path
        # made it win the primary location over real frames.
        if loc and _CODEGEN_UNIT.search(loc.group("file")):
            loc = None
        if loc:
            file = loc.group("file")
            line = int(loc.group("line")) if loc.group("line") else None
            column = int(loc.group("column")) if loc.group("column") else None
    return Frame(symbol=symbol, module=module, file=file, line=line, column=column)


def parse(text: str) -> list[Report]:
    """Every report in a TSan log, in the order they were printed."""
    reports: list[Report] = []
    current: Report | None = None
    header: str | None = None
    frames: list[Frame] = []

    def close_section() -> None:
        nonlocal header, frames
        if current is not None and header is not None:
            current.sections.append(Section(header, tuple(frames)))
        header, frames = None, []

    for raw in text.splitlines():
        line = raw.rstrip()
        warning = _WARNING.match(line) or _ERROR.match(line)
        if warning:
            close_section()
            current = Report(kind=warning.group("kind"))
            reports.append(current)
            continue
        if current is None:
            continue
        if line.startswith("SUMMARY: ThreadSanitizer:"):
            close_section()
            current.summary = line
            current = None
            continue
        if line == _SEPARATOR:
            close_section()
            current = None
            continue
        frame = _FRAME.match(line)
        if frame:
            if header is None:
                header = ""
            frames.append(_parse_frame(frame.group("rest")))
            continue
        if line.startswith("  ") and not line.startswith("   ") and line.endswith(":"):
            close_section()
            header = line.strip().rstrip(":")
    close_section()
    return reports


def fatal_errors(text: str) -> list[str]:
    """Lines saying TSan could not run, deduplicated, in order."""
    seen: dict[str, None] = {}
    for line in text.splitlines():
        if line.lstrip().startswith(_FATAL):
            seen.setdefault(line.strip(), None)
    return list(seen)


YOURS, REACHED, EXTERNAL, HARNESS = "yours", "reached", "external", "harness"


def _is_interceptor(frame: Frame) -> bool:
    return frame.module is None or bool(_INTERCEPTOR_MODULE.match(frame.module))


def _first_real(section: Section) -> Frame | None:
    return next((f for f in section.frames if not _is_interceptor(f)), None)


# C-API functions that are not thread-safe on free-threaded builds unless the
# caller holds a critical section on the object (CPython's "free-threading"
# C-API notes): they iterate or hand out borrowed references.
_NEEDS_CRITICAL_SECTION = {
    "PyDict_Next",
    "PyDict_GetItem",
    "PyDict_GetItemString",
    "PyDict_GetItemWithError",
    "PyDict_GetItem_KnownHash",
    "PyList_GetItem",
    "PyList_GET_ITEM",
    "PySequence_Fast_GET_ITEM",
    "PySequence_Fast_ITEMS",
    "PyWeakref_GetObject",
    "PyWeakref_GET_OBJECT",
}


def _rewrites_contents(section: Section, extension_modules: set[str]) -> bool:
    """This stack only rewrites a buffer's contents in place.

    Either a CPython routine that does nothing else (`_CONTENT_WRITERS`), or a
    copy primitive at the top called from a library outside the interpreter —
    numpy refilling an array with `memmove`. CPython's own containers are left
    out of the second case: a `memmove` inside `list.insert` runs under the
    list's critical section, which an extension reading the list must take
    too. A frame of yours, or any free, realloc or resize on the stack, and it
    is not a rewrite: memory the extension reads may be gone.
    """
    frames = section.frames
    if any(f.module in extension_modules or _RELEASES.search(f.symbol) for f in frames):
        return False
    first = _first_real(section)
    if first is None:
        return False
    if first.symbol.startswith(_CONTENT_WRITERS):
        return True
    copies = bool(frames) and _is_interceptor(frames[0]) and frames[0].symbol in _COPY_PRIMITIVES
    return copies and not _INTERPRETER_LIBRARY.match(first.module or "")


def _harness_rewrite(report: Report, extension_modules: set[str]) -> bool:
    """A mutator thread only rewriting buffer contents in place."""
    names = report.thread_names()
    for section in report.access_stacks:
        tid = report.access_thread(section)
        if names.get(tid or "", "").startswith(_MUTATOR_PREFIX) and _rewrites_contents(
            section, extension_modules
        ):
            return True
    return False


def _api_called(section: Section, extension_modules: set[str]) -> str | None:
    """The C-API function your extension called on this stack, when the access
    is CPython code it called directly — an extension frame comes before any
    frame of the interpreter's eval loop — else None."""
    frames = section.frames
    for i, frame in enumerate(frames):
        if frame.module in extension_modules:
            return frames[i - 1].symbol if i > 0 else ""
        if "_PyEval_EvalFrame" in frame.symbol:
            return None
    return None


def attribute(report: Report, extension_modules: set[str]) -> str:
    """YOURS, REACHED or EXTERNAL — see the module docstring."""
    stacks = report.access_stacks
    anywhere = any(f.module in extension_modules for s in stacks for f in s.frames)
    if report.is_crash:
        return YOURS if anywhere or not stacks else EXTERNAL
    if anywhere and _harness_rewrite(report, extension_modules):
        return HARNESS
    tops = [_first_real(stack) for stack in stacks]
    if anywhere and any(t is not None and t.symbol.startswith(_ALLOCATORS) for t in tops):
        # The other side is an allocation into reused memory: your code read
        # or wrote an object after it was freed (the frames look like
        # CPython's; such reports were filed as "reached" until this rule).
        return YOURS
    for top in tops:
        if top is not None and top.module in extension_modules:
            return YOURS
    entries = [_api_called(stack, extension_modules) for stack in stacks]
    direct = [entry is not None for entry in entries]
    if direct and all(direct):
        # Every racing access is CPython code your extension called directly,
        # colliding in CPython's own state (a shared decimal context).
        return REACHED
    if any(entry is not None and entry.lstrip("_") in _NEEDS_CRITICAL_SECTION for entry in entries):
        # Your extension called a C-API that requires the caller to hold a
        # lock on free-threaded builds, and Python code raced it — PyDict_Next
        # without a critical section racing a dict.pop().
        return YOURS
    if any(direct):
        # Your extension used a C-API documented as thread-safe
        # (PyList_GetItemRef) and CPython's own code
        # raced inside it: CPython's report, not yours.
        return REACHED
    return EXTERNAL


def _rule(kind: str) -> str:
    return "tsan/" + re.sub(r"[^a-z0-9]+", "-", kind.lower()).strip("-")


def _relative(path: str, root: str) -> str | None:
    """`path` relative to `root`, or None when it lies outside it.

    Only an absolute path can be in the crate. A bare file name comes from an
    uninstrumented library's debug info (`lowlevel_strided_loops.c`); resolved
    against the working directory, which is the crate root in the image, it
    would pass for a crate file.
    """
    if not os.path.isabs(path):
        return None
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:
        return None
    return None if rel == os.pardir or rel.startswith(os.pardir + os.sep) else rel


def _is_atomic(frame: Frame) -> bool:
    """CPython's atomic helpers, inlined from `pyatomic*.h`."""
    return frame.symbol.startswith("_Py_atomic_") or bool(
        frame.file and os.path.basename(frame.file).startswith("pyatomic")
    )


def _pyo3_notes(report: Report) -> list[str]:
    """A sentence per access that happened inside PyO3's own source.

    The race stays where it was attributed, but the code that raced is PyO3's,
    and PyO3 changes what it makes atomic between releases.
    """
    notes: dict[str, None] = {}
    for section in report.access_stacks:
        top = _first_real(section)
        m = _PYO3_SOURCE.search(top.file) if top is not None and top.file else None
        if m:
            where = m.group("rest") + (f":{top.line}" if top.line else "")
            notes.setdefault(
                f" The access in `{top.symbol}` is inside {m.group('crate')} {m.group('version')} "
                f"(`{where}`); a newer PyO3 may change it.",
                None,
            )
    return list(notes)


def _thread_label(report: Report, section: Section, names: dict[str, str]) -> str | None:
    """The accessing thread's name when TSan printed one, else its id (`T5`)."""
    tid = report.access_thread(section)
    return names.get(tid, tid) if tid else None


def _named_header(report: Report, section: Section, names: dict[str, str]) -> str:
    """The access header without its address, the thread's name after its id —
    `by thread T5 (ftm-refill)` says a mutator did it; `T5` alone needs the log."""
    header = _ADDRESS.sub("", section.header)
    tid = report.access_thread(section)
    if tid and tid in names:
        header = header.replace(f"by thread {tid}", f"by thread {tid} ({names[tid]})", 1)
    return header


def _frames_from_top(section: Section) -> list[Frame]:
    """The stack from its first non-interceptor frame: where the access happened."""
    frames = section.frames
    for i, frame in enumerate(frames):
        if not _is_interceptor(frame):
            return list(frames[i : i + _MAX_FRAMES])
    return list(frames[:_MAX_FRAMES])


def _user_frames(section: Section, extension_modules: set[str], crate_root: str) -> list[Frame]:
    """The stack from the frame a person should read first.

    That is the first frame in the crate's own sources. Failing that, the first
    frame in the extension module — not good enough on its own, because with
    `-Zbuild-std` the standard library is compiled into that module, and a stack
    opening on `core::ptr::copy_nonoverlapping` points at the toolchain.
    Interceptor frames in the interpreter binary are dropped either way.
    """
    frames = section.frames
    for i, frame in enumerate(frames):
        if frame.file and _relative(frame.file, crate_root) is not None:
            return list(frames[i : i + _MAX_FRAMES])
    for i, frame in enumerate(frames):
        if frame.module in extension_modules:
            return list(frames[i : i + _MAX_FRAMES])
    return list(frames[:_MAX_FRAMES])


def _frame_json(frame: Frame, crate_root: str) -> dict:
    location = None
    if frame.file:
        location = {
            "file": _relative(frame.file, crate_root) or frame.file,
            "line": max(frame.line or 1, 1),
            "column": max(frame.column or 1, 1),
        }
    return {"symbol": frame.symbol, "location": location}


def _primary(report: Report, extension_modules: set[str], crate_root: str) -> tuple[Frame, str]:
    """The frame a person should look at first, and its display path.

    Preference: the first frame inside the crate's own sources; then the first
    frame in the extension module that has a source location (PyO3, a vendored
    C library); then the first frame in the extension module at all.
    """
    stacks = report.access_stacks
    in_crate = [
        frame
        for stack in stacks
        for frame in stack.frames
        if frame.file and _relative(frame.file, crate_root) is not None
    ]
    # A thin FFI wrapper (`src/ffi/...`) is where many different bugs pass;
    # the first in-crate frame beyond it is where this one lives (distinct
    # bugs otherwise share the wrapper's line as their primary location).
    beyond_ffi = [f for f in in_crate if "/ffi/" not in f.file]
    for frame in beyond_ffi or in_crate:
        return frame, _relative(frame.file, crate_root)  # type: ignore[return-value]
    in_module = [f for s in stacks for f in s.frames if f.module in extension_modules]
    for frame in in_module:
        if frame.file:
            return frame, frame.file
    if in_module:
        return in_module[0], in_module[0].module or "<unknown>"
    first = stacks[0].frames[0] if stacks else Frame("<unknown>", None)
    return first, first.file or first.module or "<unknown>"


def to_findings(
    reports: list[Report], extension_modules: set[str], crate_root: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split reports into (yours, reached from yours, not yours), deduplicated.

    Findings use the same JSON shape as the lint, so one SARIF file carries
    both and a consumer can tell them apart only by rule id and producer.

    The same race reported from either side of the pair is one problem: stacks
    are ordered by their top frame before the signature is taken, so "write
    races previous read" and "read races previous write" collapse.

    A report of yours is shown from your first frame; the others are shown from
    where the access actually happened, so a race inside CPython reads as one.
    """
    buckets: dict[str, dict[str, dict]] = {YOURS: {}, REACHED: {}, EXTERNAL: {}, HARNESS: {}}

    for report in reports:
        owner = attribute(report, extension_modules)
        names = report.thread_names()
        if owner == YOURS:
            # Each side from where it matters: your frame when the access was
            # yours, otherwise where it happened — a Python callback mutating
            # a dict is CPython's write, not the extension frame that called
            # the callback.
            stacks = [
                (
                    section,
                    _user_frames(section, extension_modules, crate_root)
                    if (_first_real(section) or Frame("", None)).module in extension_modules
                    else _frames_from_top(section),
                )
                for section in report.access_stacks
            ]
        else:
            stacks = [(section, _frames_from_top(section)) for section in report.access_stacks]
        stacks.sort(key=lambda s: s[1][0].symbol if s[1] else "")
        if stacks:
            frame, path = _primary(report, extension_modules, crate_root)
            # A harness race is placed at the extension's access, like one of
            # yours: the mutator's side is the harness's own code.
            if owner not in (YOURS, HARNESS):
                # The plain access is the racy one; an atomic on the other
                # side (`_Py_atomic_load_ptr`) is only where TSan noticed it.
                firsts = [s[1][0] for s in stacks if s[1]]
                frame = next((t for t in firsts if not _is_atomic(t)), firsts[0] if firsts else frame)
                path = (frame.file and (_relative(frame.file, crate_root) or frame.file)) or (
                    frame.module or "<unknown>"
                )
        else:
            frame, path = Frame("<no stack captured>", None), "<no stack captured>"
        rule = _rule(report.kind)
        tops = " / ".join(dict.fromkeys(s[1][0].symbol for s in stacks if s[1]))
        signature = f"{rule}|{path}|{tops}"

        bucket = buckets[owner]
        if signature in bucket:
            bucket[signature]["occurrences"] += 1
            continue

        # Addresses differ on every run; a message that changes when nothing
        # else has is a message nobody can diff.
        described = "; ".join(
            f"{_named_header(report, section, names)} in `{frames[0].symbol}`".lstrip()
            for section, frames in stacks
            if frames
        )
        described += "".join(_pyo3_notes(report))
        if report.is_crash and not stacks:
            described = (
                "The process crashed and TSan captured no stack. It died while your "
                "extension was being driven; rerun with the printed seed to investigate."
            )
        prefix = {
            YOURS: "",
            REACHED: "Inside CPython or another library, reached from your extension. ",
            EXTERNAL: "",
            HARNESS: "A mutator rewrote a buffer's contents while your extension read it — "
            "a race in the harness by the buffer protocol's contract, not in your code. ",
        }[owner]
        bucket[signature] = {
            "rule": rule,
            "message": f"{prefix}ThreadSanitizer: {report.kind}. {described}".rstrip(),
            "confidence": "certain" if owner == YOURS else "likely",
            "producer": "tsan",
            "symbol": frame.symbol,
            "primary": {
                "file": path,
                "line": max(frame.line or 1, 1),
                "column": max(frame.column or 1, 1),
            },
            "stacks": [
                {
                    "frames": [_frame_json(f, crate_root) for f in frames],
                    "thread": _thread_label(report, section, names),
                }
                for section, frames in stacks
            ],
            "justification": None,
            "occurrences": 1,
        }
    # One root cause reported from many stacks is one finding: merge findings
    # sharing a rule and a primary line (one bug otherwise shows up as many).
    return (
        _merge(buckets[YOURS].values()),
        _merge(buckets[REACHED].values()),
        _merge(list(buckets[EXTERNAL].values()) + list(buckets[HARNESS].values())),
    )


def _merge(findings) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for f in findings:
        key = (f["rule"], f["primary"]["file"], f["primary"]["line"])
        if key in merged:
            merged[key]["occurrences"] += f["occurrences"]
        else:
            merged[key] = f
    return list(merged.values())

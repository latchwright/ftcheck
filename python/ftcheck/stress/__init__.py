# SPDX-License-Identifier: MIT OR Apache-2.0
"""`ftcheck stress` — one shared instance, many threads, a recorded seed.

Reuses `ftcheck ci`'s preflight, instrumented build and install, then runs
`driver.py` in the TSan venv instead of the project's tests. See
docs/stress.md.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field

from ftcheck.ci import Verdict, describe_findings
from ftcheck.ci.environment import Environment
from ftcheck.ci.pipeline import Options, Outcome, collect, native_modules, prepare, runtime_env
from ftcheck.exit_codes import CLEAN, FINDINGS, UNAVAILABLE, USAGE

__all__ = ["StressOptions", "StressOutcome", "coverage", "load_config", "run", "verdict"]

DRIVER = pathlib.Path(__file__).with_name("driver.py")
_KEYS = {"modules", "factories", "args", "skip", "factories_file", "mutators"}


@dataclass
class StressOptions:
    seed: int
    budget_seconds: float = 120.0
    iterations: int = 200
    only: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)


@dataclass
class StressOutcome(Outcome):
    result: dict | None = None
    config: dict | None = None


def load_config(project: pathlib.Path) -> dict:
    """The stress configuration: `[tool.ftcheck.stress]` or `ftcheck.toml`'s `[stress]`.

    An unknown key is an error, not a silent default — a misspelt `factorys`
    table would otherwise leave every type underived and the run quietly thin.
    """
    found: dict = {}
    source = None
    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text())
        if "stress" in data.get("tool", {}).get("ftcheck", {}):
            found, source = data["tool"]["ftcheck"]["stress"], pyproject
    standalone = project / "ftcheck.toml"
    if standalone.is_file():
        data = tomllib.loads(standalone.read_text())
        if "stress" in data:
            if source is not None:
                raise ValueError(f"stress is configured in both {source.name} and ftcheck.toml")
            found, source = data["stress"], standalone
    unknown = set(found) - _KEYS
    if unknown:
        raise ValueError(f"{source}: unknown [stress] key(s) {sorted(unknown)}")
    config = {
        "modules": list(found.get("modules", [])),
        "factories": dict(found.get("factories", {})),
        "args": {k: list(v) for k, v in found.get("args", {}).items()},
        "skip": dict(found.get("skip", {})),
        "mutators": dict(found.get("mutators", {})),
        "factories_file": None,
        "source": str(source) if source else None,
    }
    if found.get("factories_file"):
        path = (project / found["factories_file"]).resolve()
        if not path.is_file():
            raise ValueError(f"{source}: factories_file {found['factories_file']!r} does not exist")
        config["factories_file"] = str(path)
    return config


def _log(message: str) -> None:
    print(f"ftcheck: {message}", file=sys.stderr, flush=True)


def run(env: Environment, opts: Options, sopts: StressOptions, config: dict) -> StressOutcome:
    out = StressOutcome()
    prepared = prepare(env, opts, out, install_runner=False)
    if prepared is None:
        return out

    out.stage = "stress"
    work = opts.work_dir
    modules = sopts.modules or config["modules"] or native_modules(prepared.wheel)
    scratch = work / "stress-cwd"
    scratch.mkdir(exist_ok=True)
    driver_cfg = {
        **config,
        "modules": modules,
        "seed": sopts.seed,
        "threads": opts.threads,
        "iterations": sopts.iterations,
        "budget_seconds": sopts.budget_seconds,
        "only": sopts.only,
        "progress_path": str(work / "stress-progress.txt"),
        "output_path": str(work / "stress-result.json"),
    }
    out.config = driver_cfg
    cfg_path = work / "stress-config.json"
    cfg_path.write_text(json.dumps(driver_cfg, indent=2))
    result_path = pathlib.Path(driver_cfg["output_path"])
    result_path.unlink(missing_ok=True)

    _log(
        f"driving {', '.join(modules)} with {opts.threads} threads, seed {sopts.seed}, "
        f"budget {sopts.budget_seconds:g}s"
    )
    log = work / "stress.log"
    progress = work / "stress-progress.txt"
    code, kind, reason = _watch(
        [prepared.python, str(DRIVER), str(cfg_path)],
        cwd=scratch,
        env=runtime_env(env, opts, prepared.tsan_dir),
        log=log,
        tsan_dir=prepared.tsan_dir,
        progress=progress,
        # Derivation happens before the budget clock matters, hence the margin.
        timeout=sopts.budget_seconds + 600,
    )
    stall = None
    last = (progress.read_text().strip() if progress.exists() else "") or "before driving began"
    if kind == "stall":
        stall = _hang_finding(last, opts.threads, sopts.seed)
    elif kind:
        out.error = f"{reason}; last step: {last}"

    text = log.read_text(errors="replace")
    out.log = "\n".join(text.splitlines()[-25:])
    if result_path.exists():
        out.result = json.loads(result_path.read_text())
    # Collect even when the driver died: a crash inside the extension is
    # exactly what TSan reports, and the report must not be lost with it.
    collect(out, prepared.tsan_dir, opts.project, text)
    if stall:
        out.findings.append(stall)
    crash = _abort_finding(code, text, last, opts.threads, sopts.seed)
    # Only when TSan itself did not already report the crash (a tsan/segv).
    if crash and not any(f["rule"] in ("tsan/segv", "tsan/abrt") for f in out.findings):
        out.findings.append(crash)
    out.findings.extend(
        panic_findings(out.result, opts.threads, sopts.seed, panic_locations(text))
    )
    if out.result is None and not out.findings:
        out.stage = "stress"
        out.error = out.error or f"the driver exited {code} without a result"
    return out


_FATAL_PYTHON = re.compile(r"^Fatal Python error: (?P<what>.*)$", re.MULTILINE)


def _abort_finding(code, log_text: str, step: str, threads: int, seed: int) -> dict | None:
    """The driver died from a signal or a fatal CPython error, as a finding.

    A `Fatal Python error` (SIGABRT) produced no finding at all on a public
    project: those runs failed only because TSan also saw races, and with the
    races suppressed the same crash read as "nothing was driven".
    """
    fatal = _FATAL_PYTHON.search(log_text)
    if not fatal and not (isinstance(code, int) and code < 0):
        return None
    if fatal:
        what = f"Fatal Python error: {fatal.group('what').strip()}"
    else:
        what = f"killed by signal {-code}"
    return {
        "rule": "stress/crash",
        "message": (
            f"The process died while driving your extension from {threads} threads ({what}), "
            f"during `{step}`. Replay with --replay {seed} --threads {threads}."
        ),
        "confidence": "certain",
        "producer": "stress",
        "symbol": step,
        "primary": {"file": "<stress driver>", "line": 1, "column": 1},
        "stacks": [],
        "justification": None,
        "occurrences": 1,
    }


def _hang_finding(step: str, threads: int, seed: int) -> dict:
    """A drive step that stopped moving, as a finding.

    Not a TSan report — TSan does not detect deadlocks between a lock and a
    thread pool — but a failure observed under concurrent calls, with the pair
    that produced it and the seed that replays it.
    """
    what = step.split(" ", 1)[1] if " " in step else step
    return {
        "rule": "stress/hang",
        "message": (
            f"Calls stopped making progress while driving {what} from {threads} threads "
            f"(no progress for {STALL_SECONDS}s): a possible deadlock. Replay with "
            f"--replay {seed} --threads {threads}, and skip the callable if it blocks by design."
        ),
        "confidence": "certain",
        "producer": "stress",
        "symbol": what,
        "primary": {"file": "<stress driver>", "line": 1, "column": 1},
        "stacks": [],
        "justification": None,
        "occurrences": 1,
    }


# `thread '<name>' (<tid>) panicked at <file>:<line>:<column>:` then the
# message. Older Rust prints no thread id.
_PANIC_AT = re.compile(
    r"(?:thread '[^'\n]*'(?: \((?P<tid>\d+)\))? )?"
    r"panicked at (?P<file>[^\n]+?):(?P<line>\d+):(?P<column>\d+):\n(?P<message>[^\n]*)"
)
_BACKTRACE_FRAME = re.compile(r"^\s+\d+: (?P<symbol>\S.*)$")
_BACKTRACE_AT = re.compile(r"^\s+at (?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?$")
_REGISTRY = re.compile(r"/registry/src/[^/]+/(?P<crate>[A-Za-z0-9_.-]+?)-(?P<ver>\d+\.\d+\.\d+[^/]*)/")
_GIT_CHECKOUT = re.compile(r"/git/checkouts/(?P<crate>[^/]+?)-[0-9a-f]+/")
_STD = re.compile(r"^/rustc/|/lib/rustlib/src/rust/library/")
# How far past a thread's last matched panic line the next one is looked for.
# Extra lines on a thread (a panic the extension caught itself) are skipped.
_JOIN_WINDOW = 16


def panic_locations(log_text: str) -> list[dict]:
    """Every Rust panic line in the log, in order: where, on which thread, and
    the frames if a backtrace follows (only when RUST_BACKTRACE is set).

    `PanicException` carries the message but not the location; Rust prints
    both to stderr, which the driver log captures, with the native thread id
    the driver also records for each panicking call.
    """
    found = []
    matches = list(_PANIC_AT.finditer(log_text))
    for i, m in enumerate(matches):
        tail = log_text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(log_text)]
        found.append(
            {
                "tid": int(m.group("tid")) if m.group("tid") else None,
                "file": m.group("file"),
                "line": int(m.group("line")),
                "column": int(m.group("column")),
                "message": m.group("message").strip(),
                "frames": _backtrace(tail),
            }
        )
    return found


def _backtrace(tail: str) -> list[dict]:
    lines = tail.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines[:4]) if ln.strip() == "stack backtrace:")
    except StopIteration:
        return []
    frames = []
    for line in lines[start + 1 :]:
        frame, at = _BACKTRACE_FRAME.match(line), _BACKTRACE_AT.match(line)
        if frame:
            frames.append({"symbol": frame.group("symbol").strip(), "location": None})
        elif at and frames:
            file = at.group("file")
            frames[-1]["location"] = {
                "file": file.removeprefix("./"),
                "line": int(at.group("line")),
                "column": int(at.group("column") or 1),
            }
        else:
            break
    return frames


def _dependency(path: str) -> str | None:
    """"pyo3 0.29.2" when `path` is a dependency's source, else None (yours)."""
    if m := _REGISTRY.search(path):
        return f"{m.group('crate')} {m.group('ver')}"
    if m := _GIT_CHECKOUT.search(path):
        return m.group("crate")
    if _STD.search(path):
        return "the Rust standard library"
    return None


def _short(path: str) -> str:
    """A dependency's file from its crate directory on: `pyo3-0.29.2/src/x.rs`."""
    m = _REGISTRY.search(path) or _GIT_CHECKOUT.search(path)
    return path[m.start("crate") :] if m else path


def _same_message(logged: str, raised: str) -> bool:
    # The log holds the first line of the message; the driver keeps 300 characters.
    return logged[:300] == raised.split("\n", 1)[0].strip()


def _join(result: dict, events: list[dict], wanted: set[str]) -> tuple[dict, set[int]]:
    """(qual -> {event index of the site: panics there}, consumed event indices).

    Each thread's panicking calls, in order, are matched to that thread's
    panic lines, in order, so a site is known per call rather than guessed
    from the message.
    """
    by_tid: dict[int, list[int]] = {}
    for i, e in enumerate(events):
        if e["tid"] is not None:
            by_tid.setdefault(e["tid"], []).append(i)
    located: dict[str, dict[int, int]] = {}
    consumed: set[int] = set()
    for tid, runs in (result.get("panic_threads") or {}).items():
        mine = by_tid.get(int(tid), [])
        k = 0
        for qual, message, count in runs:
            for _ in range(count):
                j = next(
                    (
                        j
                        for j in range(k, min(k + _JOIN_WINDOW, len(mine)))
                        if _same_message(events[mine[j]]["message"], message)
                    ),
                    None,
                )
                if j is None:
                    continue
                k = j + 1
                consumed.add(mine[j])
                if qual in wanted:
                    site = located.setdefault(qual, {})
                    site[mine[j]] = site.get(mine[j], 0) + 1
    return located, consumed


def _site_key(event: dict) -> str:
    return f"{event['file']}:{event['line']}"


def panic_findings(
    result: dict | None, threads: int, seed: int, locations: list[dict] | None = None
) -> list[dict]:
    """Rust panics a callable raised only when driven from many threads.

    Not a data race — TSan may see nothing — but a panic that the single-threaded
    baseline never produced is contention the code does not handle. Hidden,
    a run in which concurrent calls panicked read as "no race detected". A
    panic the baseline also raised is how the method behaves
    with those arguments, and is left alone.

    One finding per source location, naming every callable that panicked
    there with its own count: one `borrow()` shared by many methods produces
    the same panic in each of them. The site of each panic comes from joining
    the driver's per-thread record with the thread id in Rust's panic line.
    Without thread ids (older Rust), a panic is located by its message, and
    when several sites share the message the finding names them all.
    """
    if not result:
        return []
    events = locations or []
    totals: dict[str, int] = {}
    for group in result.get("groups", []):
        serial = group.get("serial_exceptions", {})
        for qual in group.get("driven", []):
            n = result.get("exceptions", {}).get(qual, {}).get("PanicException", 0)
            if n and "PanicException" not in serial.get(qual, []):
                totals[qual] = n
    located, consumed = _join(result, events, set(totals))

    groups: dict[str, dict] = {}

    def add(key, sites, message, qual, n):
        entry = groups.setdefault(key, {"sites": sites, "message": message, "callables": {}})
        entry["callables"][qual] = entry["callables"].get(qual, 0) + n

    for qual, n in totals.items():
        for index, count in located.get(qual, {}).items():
            event = events[index]
            add(_site_key(event), [event], event["message"], qual, count)
        rest = n - sum(located.get(qual, {}).values())
        if rest <= 0:
            continue
        # Not joined to a line: located by message, naming every candidate site.
        raised = result.get("panics", {}).get(qual) or {
            result.get("messages", {}).get(qual, {}).get("PanicException", ""): rest
        }
        pool = [e for i, e in enumerate(events) if i not in consumed] or events
        sites = {}
        for message in raised:
            for e in pool:
                if _same_message(e["message"], message):
                    sites.setdefault(_site_key(e), e)
        first = next(iter(raised), "")
        if not sites:
            add(f"message:{first}", [], first, qual, rest)
        else:
            ordered = [sites[k] for k in sorted(sites)]
            add(" | ".join(sorted(sites)), ordered, ordered[0]["message"] or first, qual, rest)

    calls = result.get("calls", {})
    return [_panic_finding(entry, calls, threads, seed) for entry in groups.values()]


def _inside(dependency: str, site: dict) -> str:
    where = f"{_short(site['file'])}:{site['line']}"
    if dependency.startswith("pyo3 ") and (
        "/conversions/" in site["file"] or "extract_argument" in site["file"]
    ):
        return f" inside PyO3's argument conversion ({dependency}, a dependency) at {where}"
    if dependency.startswith("the Rust"):
        return f" inside {dependency} at {where}"
    return f" inside the dependency {dependency} at {where}"


def _your_frame(frames: list[dict]) -> tuple[dict | None, list[dict]]:
    """The first backtrace frame in the crate's own Rust code, and the stack
    from the first frame outside the standard library down to it."""
    mine = [
        i
        for i, fr in enumerate(frames)
        if fr["location"]
        and fr["location"]["file"].endswith(".rs")
        and _dependency(fr["location"]["file"]) is None
    ]
    if not mine:
        return None, []
    first = mine[0]
    start = next(
        (i for i, fr in enumerate(frames) if fr["location"] and not _STD.search(fr["location"]["file"])),
        first,
    )
    return frames[first], [{"frames": frames[start : first + 1]}]


def _panic_finding(entry: dict, calls: dict, threads: int, seed: int) -> dict:
    callables = entry["callables"]
    names = list(callables)
    total = sum(callables.values())
    n_calls = sum(calls.get(q, 0) for q in names)
    if len(names) == 1:
        named = f"{total} of {n_calls} calls to `{names[0]}`"
    else:
        each = [f"`{q}` ({callables[q]} of {calls.get(q, 0)})" for q in names[:5]]
        more = f" and {len(names) - 5} more" if len(names) > 5 else ""
        named = f"{total} of {n_calls} calls to {', '.join(each)}{more}"
    sites = entry["sites"]
    dependency = _dependency(sites[0]["file"]) if len(sites) == 1 else None
    yours, stacks, reached, hint = None, [], "", ""
    if dependency:
        at = _inside(dependency, sites[0])
        yours, stacks = _your_frame(sites[0]["frames"])
        if yours:
            where = yours["location"]
            reached = f" It was reached from `{yours['symbol']}` at {where['file']}:{where['line']}."
        else:
            hint = " (with RUST_BACKTRACE=1 set, to see which of your frames led there)"
    elif len(sites) == 1:
        at = f" at {_site_key(sites[0])}"
    elif sites:
        at = f" at one of {', '.join(_site_key(e) for e in sites)} (the log does not say which)"
    else:
        at = ""
    if yours:
        primary = dict(yours["location"])
    elif sites:
        primary = {k: sites[0][k] for k in ("file", "line", "column")}
    else:
        primary = {"file": "<stress driver>", "line": 1, "column": 1}
    finding = {
        "rule": "stress/panic",
        "message": (
            f"Panicked{at} in {named} when driven from {threads} threads, and never in the "
            f"single-threaded baseline: {entry['message'] or '(no message)'}.{reached}"
            f" Replay with --replay {seed} --threads {threads}{hint}."
        ),
        "confidence": "certain",
        "producer": "stress",
        "symbol": names[0],
        "primary": primary,
        "stacks": stacks,
        "justification": None,
        "occurrences": len(names),
    }
    if dependency:
        finding["dependency"] = dependency
    return finding


def _crash_logged(tsan_dir: pathlib.Path) -> bool:
    for path in tsan_dir.glob("tsan*"):
        try:
            if "ERROR: ThreadSanitizer:" in path.read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


# How long the driver may sit on one step of the drive phase before it counts
# as hung. A pair phase is a few hundred calls per thread; five minutes without
# finishing one is a deadlock, not a slow call.
STALL_SECONDS = 300


def _watch(cmd, cwd, env, log, tsan_dir, progress, timeout):
    """Run the driver; return (exit code, kind, reason) — kind is None when it
    finished, else "crash", "stall" or "timeout".

    After a crash TSan sometimes never finishes printing its report (seen on a
    public project, spinning at full CPU), so once a crash is in the log it gets
    a short grace period. A drive step that stops moving is a hang. And the
    whole run has a wall-clock ceiling.
    """
    started = time.monotonic()
    crash_seen_at = None
    last_step, step_since = None, started
    with open(log, "w") as fh:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)

        def stop(kind, reason):
            proc.kill()
            proc.wait()
            return None, kind, reason

        while True:
            try:
                return proc.wait(timeout=5), None, None
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if crash_seen_at is None and _crash_logged(tsan_dir):
                crash_seen_at = now
            if crash_seen_at is not None and now - crash_seen_at > 60:
                return stop("crash", "the process crashed and TSan did not finish reporting it")
            step = progress.read_text().strip() if progress.exists() else None
            if step != last_step:
                last_step, step_since = step, now
            elif step and step.startswith(("pair ", "mix ")) and now - step_since > STALL_SECONDS:
                return stop("stall", f"no progress for {STALL_SECONDS}s during `{step}`")
            if now - started > timeout:
                return stop("timeout", f"the driver ran past its {timeout:g}s ceiling")


def confined(result: dict | None) -> int:
    """Callables on thread-confined (`unsendable`) types: nothing to race."""
    if not result:
        return 0
    return sum(len(g.get("confined_callables", [])) for g in result["groups"])


def coverage(result: dict | None) -> tuple[int, int]:
    """(callables driven, callables on the surface that could race).

    Thread-confined callables are left out of both: PyO3 prevents them from
    being shared, so they are neither exercised nor a hole.
    """
    if not result:
        return 0, 0
    driven = total = 0
    returned = result.get("returned")
    for group in result["groups"]:
        total += len(group["driven"]) + len(group["not_driven"])
        if not group["pairs_driven"]:
            continue
        for qual in group["driven"]:
            # A callable whose every call raised reached only its argument
            # checks, not its body: it is not counted as driven.
            if returned is None or returned.get(qual, 0) > 0:
                driven += 1
    return driven, total


def verdict(env: Environment, outcome: StressOutcome | None) -> Verdict:
    if not env.ok:
        names = ", ".join(c.name for c in env.failures)
        return Verdict(UNAVAILABLE, f"could not run: the environment failed preflight ({names})")
    if outcome is None or outcome.stage != "done":
        stage = outcome.stage if outcome else "preflight"
        why = outcome.error if outcome and outcome.error else "unknown failure"
        return Verdict(UNAVAILABLE, f"could not run: {why} (stage: {stage})")
    if outcome.findings:
        return Verdict(FINDINGS, describe_findings(outcome.findings))
    # After a confirmed finding, a TSan abort (often a heap the race already
    # corrupted) does not undo it; before any finding, it means nothing ran.
    if outcome.fatal:
        return Verdict(UNAVAILABLE, "could not run: ThreadSanitizer aborted — " + outcome.fatal[0])
    unmatched = (outcome.result or {}).get("only_unmatched")
    if unmatched:
        names = ", ".join(outcome.result.get("available", [])[:20]) or "none"
        return Verdict(
            USAGE, f"--only matched nothing ({', '.join(unmatched)}); types and modules: {names}"
        )
    driven, total = coverage(outcome.result)
    n_confined = confined(outcome.result)
    if driven == 0 and total == 0 and n_confined:
        return Verdict(
            CLEAN,
            f"no race possible on the exercised surface: all {n_confined} callables belong to "
            "#[pyclass(unsendable)] types, which PyO3 confines to one thread",
        )
    if driven == 0:
        return Verdict(
            UNAVAILABLE,
            f"could not run: nothing was driven (0 of {total} callables); "
            "see the coverage report for why",
        )
    return Verdict(
        CLEAN,
        f"no race detected on the exercised surface ({driven} of {total} callables driven)",
    )


def default_seed() -> int:
    return int.from_bytes(os.urandom(4), "big")

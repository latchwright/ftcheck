# SPDX-License-Identifier: MIT OR Apache-2.0
"""The `ftcheck stress` driver.

Runs inside the project's ThreadSanitizer venv, where ftcheck itself is not
installed, so it is one file with no imports beyond the standard library. It
reads a JSON config, drives the native modules' surface from many threads
against one shared instance per type, and writes a JSON result.

    python driver.py CONFIG.json

The orchestrator (`ftcheck.stress`) builds the config, runs this under TSan,
and reads the result together with the TSan logs.
"""
# Catching every exception is the job here, not a shortcut: the driver calls
# arbitrary code with guessed arguments and must survive whatever it raises.
# `exec` loads the project's own factories file, which is trusted input.
# ruff: noqa: BLE001, S102
from __future__ import annotations

import contextlib
import contextvars
import importlib
import inspect
import itertools
import json
import os
import random
import sys
import threading
import time
import traceback
import types

# Dunders worth driving, and how to invoke each on an instance.
_DUNDERS = {
    "__len__": lambda obj: len(obj),
    "__repr__": lambda obj: repr(obj),
    "__iter__": lambda obj: list(iter(obj)),
    "__contains__": lambda obj, x: x in obj,
    "__getitem__": lambda obj, k: obj[k],
}

# The primitive pool derivation draws from, in preference order.
_POOL = {
    "int": [1, 0, 2, 16, -1],
    "str": ["a", "", "key", "ftcheck"],
    "bytes": [b"a", b"", b"\x00\x01"],
    "float": [1.0, 0.0, 0.5],
    "bool": [True, False],
    "none": [None],
    "list": [[], [1, 2]],
    "dict": [{}, {"a": 1}],
}

# Parameter-name hints: which pool kinds to try first.
_HINTS = [
    (("index", "idx", "len", "size", "count", "n", "capacity", "cap", "num", "pos",
      "offset", "start", "end", "stop", "step", "width", "depth", "level", "id", "k"),
     ("int",)),
    (("name", "key", "text", "s", "string", "label", "path", "prefix", "suffix",
      "pattern", "sep", "encoding", "mode", "fmt", "format", "url", "word"),
     ("str",)),
    (("data", "buf", "buffer", "bytes", "b", "payload", "raw", "blob"), ("bytes",)),
    (("value", "val", "v", "x", "item", "obj", "elem", "element"),
     ("int", "str", "float", "bytes")),
    (("flag", "enabled", "strict", "reverse", "verbose"), ("bool",)),
    (("items", "values", "seq", "iterable", "elements", "keys"), ("list",)),
    (("mapping", "kwargs", "options", "config", "map"), ("dict",)),
]
_DEFAULT_ORDER = ("int", "str", "bytes", "float", "bool", "list", "dict", "none")

# PyO3 turns a Rust panic into `pyo3_runtime.PanicException`, which derives from
# BaseException so that `except Exception` does not swallow it. The driver must
# survive it — a panicking probe ended whole runs on two public projects — so
# everything is caught except a real interrupt or exit.
_PASS_THROUGH = (KeyboardInterrupt, SystemExit)

_MAX_COMBOS = 64      # derivation attempts per callable
_MAX_ARGSETS = 3      # distinct argument tuples kept per callable
_MAX_PANIC_MESSAGES = 20   # distinct panic messages kept per callable
_MAX_PANIC_RUNS = 256      # changes of (callable, message) recorded per thread
_MAX_PANIC_RUNS_TOTAL = 20000  # and in the whole run, to bound the result file


class Progress:
    """Writes what the driver is about to do, so a hang can be attributed."""

    def __init__(self, path):
        self.path = path

    def say(self, message):
        if not self.path:
            return
        with open(self.path, "w") as fh:
            fh.write(message + "\n")


def _kinds_for(param_name):
    name = param_name.lower().lstrip("_")
    for names, kinds in _HINTS:
        if name in names:
            return list(kinds) + [k for k in _DEFAULT_ORDER if k not in kinds]
    return list(_DEFAULT_ORDER)


def _required_params(callable_obj, bound):
    """Names of required positional parameters, or None if unknowable."""
    try:
        sig = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return None
    params = list(sig.parameters.values())
    if bound and params and params[0].name in ("self", "$self", "cls"):
        params = params[1:]
    required = []
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is p.empty and p.kind != p.KEYWORD_ONLY:
            required.append(p.name)
    return required


def _candidates(params):
    """Argument tuples to try, most plausible first, capped."""
    per_param = []
    for name in params:
        values = []
        for kind in _kinds_for(name):
            values.extend(_POOL[kind])
        per_param.append(values)
    return itertools.islice(itertools.product(*per_param), _MAX_COMBOS)


class Surface:
    """What the native modules expose, and how each piece can be reached."""

    def __init__(self, cfg, progress):
        self.cfg = cfg
        self.progress = progress
        self.namespace = {}
        self.modules = []
        # The configured native modules: public aliases are computed against
        # these only, so a submodule member keeps its submodule segment.
        self.roots = list(cfg["modules"])
        self.types = []        # (qualname, cls)
        self.functions = []    # (qualname, fn)
        self.errors = []

    def load(self):
        for name in self.cfg["modules"]:
            self.progress.say(f"import {name}")
            try:
                module = importlib.import_module(name)
            except _PASS_THROUGH:
                raise
            except BaseException as exc:  # the import itself is part of the surface
                self.errors.append({"module": name, "error": f"{type(exc).__name__}: {exc}"})
                continue
            self.modules.append((name, module))
            self.namespace[name.split(".")[0]] = importlib.import_module(name.split(".")[0])
            self.namespace[name.replace(".", "_")] = module
            self._collect(name, module)

        factories_file = self.cfg.get("factories_file")
        self.py_factories, self.py_args, self.py_mutators = {}, {}, {}
        if factories_file:
            # A registered module, not a bare namespace: `@dataclass` and
            # pickling look the defining module up in sys.modules.
            module = types.ModuleType("ftcheck_factories")
            module.__file__ = factories_file
            sys.modules["ftcheck_factories"] = module
            with open(factories_file) as fh:
                exec(compile(fh.read(), factories_file, "exec"), module.__dict__)
            self.py_factories = dict(getattr(module, "FACTORIES", {}))
            self.py_args = dict(getattr(module, "ARGS", {}))
            self.py_mutators = dict(getattr(module, "MUTATORS", {}))

    def _collect(self, name, module):
        """Public classes and functions of one native module, and its submodules.

        PyO3 submodules (`m.add_submodule`) are module objects with no file of
        their own. Skipping them hid most of one public project's API and
        overstated coverage, so they are walked too.
        """
        seen = set()
        for attr in sorted(dir(module)):
            if attr.startswith("_"):
                continue
            obj = getattr(module, attr)
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            qual = f"{name}.{attr}"
            if isinstance(obj, types.ModuleType):
                own = getattr(obj, "__file__", None)
                native = own is None or own == getattr(module, "__file__", None)
                if native and all(m is not obj for _, m in self.modules):
                    self.modules.append((qual, obj))
                    self._collect(qual, obj)
                continue
            if isinstance(obj, type):
                self.types.append((qual, obj))
            elif type(obj).__name__ in ("builtin_function_or_method", "function"):
                self.functions.append((qual, obj))

    def evaluate(self, expr):
        return eval(expr, dict(self.namespace))

    def evaluate_call(self, expr):
        """`"1, 'a', strict=True"` -> ((1, 'a'), {'strict': True})."""
        scope = dict(self.namespace)
        scope["__ftcheck_capture"] = lambda *a, **k: (a, k)
        return eval(f"__ftcheck_capture({expr})", scope)

    def keys(self, qual):
        """Names a config may use for `qual`: its full path, and its public one.

        maturin puts the native module inside a package of the same name and
        re-exports it, so `pkg.pkg.Tally` is what users import as `pkg.Tally`.
        Either spelling is accepted.
        """
        out = [qual]
        for name in self.roots:
            if (qual == name or qual.startswith(name + ".")) and "." in name:
                out.append(name.split(".")[0] + qual[len(name):])
        return out

    def lookup(self, mapping, qual):
        for key in self.keys(qual):
            if key in mapping:
                return mapping[key]
        return None


def _variants(cls):
    """(name, value) for class attributes that are instances of the class."""
    out = []
    for name, value in vars(cls).items():
        if not name.startswith("_") and isinstance(value, cls):
            out.append((name, value))
    return out


def _is_builtin_type(base):
    import builtins

    return base.__module__ == "builtins" and getattr(builtins, base.__name__, None) is base


def _members(cls):
    """(name, kind, raw) for the public, drivable members of a class.

    Inherited members count: a `#[pyclass(extends=Base)]` whose base is not
    exported otherwise showed no callables at all, and a run reported "no race"
    without calling the main API (seen on a public project). Python's own
    builtin types (object, Exception, ...) are not part of the surface.
    """
    out = []
    seen = set()
    for base in cls.__mro__:
        if base is object or _is_builtin_type(base):
            continue
        for name, raw in vars(base).items():
            if name in seen:
                continue
            seen.add(name)
            out.extend(_classify(cls, name, raw))
    return sorted(out, key=lambda m: m[0])


def _classify(cls, name, raw):
    out = []
    if isinstance(raw, cls):
        return out  # an enum variant: a value, not something to call
    tname = type(raw).__name__
    if name in _DUNDERS:
        out.append((name, "dunder", raw))
    elif name.startswith("_"):
        return out
    elif tname in ("method_descriptor", "function", "builtin_function_or_method"):
        out.append((name, "method", raw))
    elif tname in ("getset_descriptor", "property"):
        out.append((name, "property", raw))
    elif isinstance(raw, (staticmethod, classmethod)) or tname == "classmethod_descriptor":
        # PyO3's #[classmethod] and #[staticmethod], called through the type.
        out.append((name, "static", raw))
    return out


class Plan:
    """For one group — a type, or a module's functions — what will be driven."""

    def __init__(self, name, kind):
        self.name = name
        self.kind = kind
        self.factory = None          # description of where the instance came from
        self.factory_error = None
        self.make = None             # zero-arg callable producing a fresh instance
        self.calls = []              # (qualname, fn(instance, args), [argtuples])
        self.not_driven = {}         # qualname -> reason
        self.confined = None         # why the type cannot be shared, if it cannot
        self.serial = {}             # qualname -> exception names seen single-threaded


def _call_member(name, kind, raw):
    """A function (instance, args) -> result invoking one member."""
    if kind == "dunder":
        fn = _DUNDERS[name]
        return lambda obj, args, kwargs: fn(obj, *args)
    if kind == "property":
        return lambda obj, args, kwargs: getattr(obj, name)
    if kind == "static":
        return lambda obj, args, kwargs: getattr(type(obj), name)(*args, **kwargs)
    return lambda obj, args, kwargs: getattr(obj, name)(*args, **kwargs)


def _has_setter(make, name):
    try:
        instance = make()
        setattr(instance, name, getattr(instance, name))
        return True
    except _PASS_THROUGH:
        raise
    except BaseException:
        return False


def _setter(name):
    return lambda obj, args, kwargs: setattr(obj, name, getattr(obj, name))


def _declared_args(surface, declared_args, qual):
    """Declared argument tuples for `qual`, or None to derive them."""
    exprs = surface.lookup(declared_args, qual)
    if exprs is not None:
        return [surface.evaluate_call(e) for e in exprs]
    py = surface.lookup(surface.py_args, qual)
    if py is not None:
        return [_as_call(a) for a in py]
    return None


def _as_call(value):
    """An ARGS entry: a tuple, a kwargs dict, or an (args, kwargs) pair."""
    if isinstance(value, dict):
        return (), dict(value)
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], tuple)
        and isinstance(value[1], dict)
    ):
        return value
    return tuple(value), {}


def _format_call(call):
    args, kwargs = call
    parts = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
    return "(" + ", ".join(parts) + ")"


def _constructor(cls, args):
    return lambda: cls(*args)


def _module_call(fn_obj):
    return lambda _obj, args, kwargs: fn_obj(*args, **kwargs)


def _derive_args(qual, fn, probe_target, params, declared, progress, stats):
    """Argument tuples for one callable, and a reason when there are none."""
    if declared is not None:
        return declared, None
    if params is None:
        params = []  # unknown signature: try the no-argument call first
    if not params:
        combos = [()]
    else:
        combos = list(_candidates(params))
    combos = [(c, {}) for c in combos]
    found, returned = [], []
    last_error = None
    for args in combos:
        progress.say(f"probe {qual}{_format_call(args)}")
        try:
            target = probe_target()
            fn(target, *args)
            returned.append(args)
        except TypeError as exc:
            last_error = f"TypeError: {exc}"
            continue
        except _PASS_THROUGH:
            raise
        except BaseException as exc:  # a body was reached: arguments were accepted
            found.append(args)
            stats[qual] = stats.get(qual, 0) + 1
            last_error = f"{type(exc).__name__}: {exc}"
        if len(returned) >= _MAX_ARGSETS:
            break
    chosen = (returned + found)[:_MAX_ARGSETS]
    if chosen:
        return chosen, None
    why = f"no argument tuple accepted for parameters {params}"
    return None, f"{why} ({last_error})" if last_error else why


def plan_type(surface, qual, cls, cfg, progress):
    plan = Plan(qual, "type")
    skip = cfg.get("skip", {})
    declared_args = cfg.get("args", {})

    # --- the factory -----------------------------------------------------
    expr = surface.lookup(cfg.get("factories", {}), qual)
    py_factory = surface.lookup(surface.py_factories, qual)
    if expr is not None:
        plan.factory = f"declared: {expr}"
        plan.make = lambda: surface.evaluate(expr)
    elif py_factory is not None:
        plan.factory = f"declared in {cfg.get('factories_file')}"
        plan.make = py_factory
    elif _variants(cls):
        # A PyO3 enum cannot be constructed, but its variants are class
        # attributes that are instances of it.
        name, variant = _variants(cls)[0]
        plan.factory = f"derived: {cls.__name__}.{name}"
        plan.make = lambda: variant
    else:
        params = _required_params(cls, bound=False)
        if params is None:
            params = []
        for args in _candidates(params) if params else [()]:
            progress.say(f"construct {qual}{args!r}")
            try:
                cls(*args)
            except _PASS_THROUGH:
                raise
            except BaseException as exc:
                plan.factory_error = f"{type(exc).__name__}: {exc}"
                continue
            plan.factory = f"derived: {cls.__name__}{args!r}".replace(",)", ")")
            plan.make = _constructor(cls, args)
            break
        if plan.make is None:
            if plan.factory_error and "cannot create" in plan.factory_error:
                reason = (
                    "no Python constructor: the type has no #[new], so instances only come "
                    "from other calls. Declare a factory that obtains one."
                )
            else:
                reason = (
                    f"no factory: constructor parameters {params} could not be satisfied "
                    f"from primitives ({plan.factory_error})"
                )
            for name, _kind, _raw in _members(cls):
                plan.not_driven[f"{qual}.{name}"] = reason
            return plan

    try:
        progress.say(f"factory {qual}")
        plan.make()
    except _PASS_THROUGH:
        raise
    except BaseException as exc:
        reason = f"factory raised {type(exc).__name__}: {exc}"
        for name, _kind, _raw in _members(cls):
            plan.not_driven[f"{qual}.{name}"] = reason
        plan.make = None
        return plan

    # --- the members ------------------------------------------------------
    stats = {}
    members = _members(cls)
    # A private method is surface when the config declares arguments for it:
    # that is how to reach an `unsafe` block kept behind an underscore.
    public = {m[0] for m in members}
    for base in cls.__mro__:
        for name, raw in vars(base).items():
            private = name.startswith("_") and not name.startswith("__")
            declared = (
                surface.lookup(declared_args, f"{qual}.{name}") is not None
                or surface.lookup(surface.py_args, f"{qual}.{name}") is not None
            )
            if private and declared and name not in public:
                members.append((name, "method", raw))
                public.add(name)
    for name, kind, raw in members:
        mqual = f"{qual}.{name}"
        why_skip = surface.lookup(skip, mqual)
        if why_skip is not None:
            plan.not_driven[mqual] = f"skipped: {why_skip}"
            continue
        fn = _call_member(name, kind, raw)
        declared = _declared_args(surface, declared_args, mqual)
        if kind == "property":
            params = []
        elif kind == "dunder":
            params = {"__contains__": ["x"], "__getitem__": ["index"]}.get(name, [])
        else:
            params = _required_params(getattr(cls, name), bound=(kind == "method"))
        argsets, reason = _derive_args(mqual, fn, plan.make, params, declared, progress, stats)
        if argsets is None:
            plan.not_driven[mqual] = reason
            continue
        plan.calls.append((mqual, fn, argsets))
        if kind == "property" and _has_setter(plan.make, name):
            # Driven as its own callable: writing the value just read back.
            # Setters were never driven while coverage read 100% (public
            # project); a read racing a write is exactly the case to reach.
            plan.calls.append((f"{mqual} (set)", _setter(name), [((), {})]))
    if plan.calls:
        _serial_baseline(plan, progress)
        progress.say(f"confinement {qual}")
        plan.confined = _confinement(plan)
    return plan


def _serial_baseline(plan, progress):
    """Call every argument tuple three times, on one thread, and note what it raises.

    The baseline for "only under concurrency": a panic a method throws here is
    how it behaves, not a race; one it throws only when driven from many
    threads is a finding (an exclusive borrow that one of two threads loses).
    """
    for qual, fn, argsets in plan.calls:
        seen = set()
        instance = plan.make()
        # Several sequential calls on one instance: a panic on the second call
        # (a duplicate insert, a lock poisoned by the first panic) is how the
        # method behaves on one thread, not contention — with a fresh instance
        # per call it would be misreported as concurrency-only.
        for _ in range(3):
            for args, kwargs in argsets:
                progress.say(f"baseline {qual}")
                try:
                    fn(instance, args, kwargs)
                except _PASS_THROUGH:
                    raise
                except BaseException as exc:
                    seen.add(type(exc).__name__)
        plan.serial[qual] = sorted(seen)


def _confinement(plan):
    """Why instances of this type cannot be used from a second thread, or None.

    PyO3 checks every access to a `#[pyclass(unsendable)]` object and raises
    when it comes from any thread but the creator. Such a type cannot race
    across Python threads, so it is reported as confined, not as a hole in
    coverage and not as a failure.
    """
    _qual, fn, argsets = plan.calls[0]
    instance = plan.make()
    seen = []

    def probe():
        try:
            fn(instance, *argsets[0])
        except BaseException as exc:
            seen.append(f"{type(exc).__name__}: {exc}")

    t = threading.Thread(target=probe, name="ftc-confine")
    t.start()
    t.join()
    if seen and "unsendable" in seen[0].lower():
        return f"thread-confined: PyO3 raised on use from another thread ({seen[0][:120]})"
    return None


def plan_functions(surface, module_name, functions, cfg, progress):
    plan = Plan(f"{module_name} (module functions)", "functions")
    plan.factory = "module state"
    plan.make = lambda: None
    skip = cfg.get("skip", {})
    declared_args = cfg.get("args", {})
    stats = {}
    for qual, fn_obj in functions:
        why_skip = surface.lookup(skip, qual)
        if why_skip is not None:
            plan.not_driven[qual] = f"skipped: {why_skip}"
            continue
        fn = _module_call(fn_obj)
        declared = _declared_args(surface, declared_args, qual)
        params = _required_params(fn_obj, bound=False)
        argsets, reason = _derive_args(qual, fn, plan.make, params, declared, progress, stats)
        if argsets is None:
            plan.not_driven[qual] = reason
            continue
        plan.calls.append((qual, fn, argsets))
    if plan.calls:
        _serial_baseline(plan, progress)
    return plan


def _fresh_context():
    """Each worker gets an empty contextvars context.

    On free-threaded 3.14 a new thread inherits a copy of its parent's context,
    and a copy shares the same values: every worker would share, say, the main
    thread's `decimal` context object, and its races would be reported as the
    extension's. Found on a public project. (`context=` is new in 3.14.)
    """
    if "context" in inspect.signature(threading.Thread).parameters:
        return {"context": contextvars.Context()}
    return {}


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = {}
        self.returned = {}
        self.exceptions = {}
        self.messages = {}      # qual -> {exception name: first message}
        self.panics = {}        # qual -> {panic message: count}
        # Native thread id -> [[qual, message, count], ...], in call order. Rust
        # prints the same id in its panic line, so the orchestrator can tell
        # which call panicked at which site: two sites with one message were
        # otherwise indistinguishable (one was filed under the other).
        self.panic_threads = {}
        self._panic_full = set()
        self._panic_runs = 0
        self.total = 0          # every call, for progress: a hang is no call finishing

    def record(self, qual, exc_name, message=None, tid=None):
        with self.lock:
            self.total += 1
            self.calls[qual] = self.calls.get(qual, 0) + 1
            if not exc_name:
                self.returned[qual] = self.returned.get(qual, 0) + 1
            if exc_name:
                per = self.exceptions.setdefault(qual, {})
                per[exc_name] = per.get(exc_name, 0) + 1
                if message is not None:
                    self.messages.setdefault(qual, {}).setdefault(exc_name, message[:300])
            if exc_name == "PanicException" and message is not None:
                self._record_panic(qual, message[:300], tid)

    def _record_panic(self, qual, message, tid):
        per = self.panics.setdefault(qual, {})
        if message in per or len(per) < _MAX_PANIC_MESSAGES:
            per[message] = per.get(message, 0) + 1
        key = str(tid)
        if tid is None or key in self._panic_full:
            return
        runs = self.panic_threads.setdefault(key, [])
        if runs and runs[-1][0] == qual and runs[-1][1] == message:
            runs[-1][2] += 1
        elif len(runs) < _MAX_PANIC_RUNS and self._panic_runs < _MAX_PANIC_RUNS_TOTAL:
            self._panic_runs += 1
            runs.append([qual, message, 1])
        else:
            # Past the cap the thread records nothing more, so what it did
            # record stays in step with its panic lines. Its later panics are
            # located by message, as when no thread id is known.
            self._panic_full.add(key)


def _worker(shared, work, rng, iterations, counters, barrier, errors, deadline):
    tid = threading.get_native_id()
    try:
        barrier.wait()
        for _ in range(iterations):
            # The budget is honoured per call: one ever-slower call must not
            # hold a phase past it (seen on a public project).
            if time.monotonic() > deadline:
                break
            qual, fn, argsets = work[rng.randrange(len(work))]
            args, kwargs = argsets[rng.randrange(len(argsets))]
            exc_name = message = None
            try:
                fn(shared, args, kwargs)
            except _PASS_THROUGH:
                raise
            except BaseException as exc:
                exc_name = type(exc).__name__
                message = str(exc)
            counters.record(qual, exc_name, message, tid)
            # Seeded yield points perturb the interleaving reproducibly.
            if rng.random() < 0.05:
                time.sleep(0)
    except _PASS_THROUGH:
        raise
    except BaseException:
        errors.append(traceback.format_exc())


def _heartbeat(progress, step, counters, done, on_tick=None):
    """Rewrites the progress line with the running call count every few seconds.

    The orchestrator treats an unchanged line as a hang; with the count in it,
    a call that is merely slow keeps the line moving, and only a standstill —
    no call finishing at all — is reported (a slow call was reported as a
    deadlock three times on a public project).
    """
    interval = 1
    while not done.wait(interval):
        interval = 3
        progress.say(f"{step} | calls {counters.total}")
        if on_tick is not None:
            # Nothing may stop the heartbeat: a silent heartbeat reads as a
            # hang, and a failed snapshot is not worth a false deadlock.
            with contextlib.suppress(Exception):
                on_tick()


def _mutator(name, mutate, counters, done):
    """Runs one declared mutator in a loop until the phase ends."""
    tid = threading.get_native_id()
    while not done.is_set():
        exc_name = message = None
        try:
            mutate()
        except _PASS_THROUGH:
            raise
        except BaseException as exc:
            exc_name, message = type(exc).__name__, str(exc)
        counters.record(f"mutator:{name}", exc_name, message, tid)
        time.sleep(0)


def drive(
    plan, cfg, seed_rng, counters, progress, deadline, on_pair=None, mutators=(), on_tick=None
):
    """Pairs, then mix. Returns (pairs_driven, pairs_total, reached_deadline)."""
    threads = cfg["threads"]
    iterations = cfg["iterations"]
    calls = plan.calls
    pairs = list(itertools.combinations_with_replacement(range(len(calls)), 2))
    seed_rng.shuffle(pairs)
    shared = plan.make()
    driven = 0
    errors = []

    def run_phase(assignment, label, step):
        barrier = threading.Barrier(threads)
        workers = []
        for t in range(threads):
            rng = random.Random(seed_rng.getrandbits(64))
            workers.append(threading.Thread(
                target=_worker,
                args=(shared, assignment(t), rng, iterations, counters, barrier, errors, deadline),
                name=f"ftw-{label}-{t}",
                **_fresh_context(),
            ))
        done = threading.Event()
        side = [threading.Thread(target=_heartbeat, args=(progress, step, counters, done, on_tick),
                                 name="ftc-heartbeat", daemon=True)]
        for name, mutate in mutators:
            side.append(threading.Thread(
                target=_mutator, args=(name, mutate, counters, done),
                # "ftm-" + name, cut to Linux's 15-character thread names: TSan
                # then names the mutator (it once showed only "ftcheck-mutator").
                name=f"ftm-{name}"[:15], daemon=True, **_fresh_context(),
            ))
        for th in side:
            th.start()
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        done.set()
        for th in side:
            th.join()

    for i, j in pairs:
        if time.monotonic() > deadline:
            return driven, len(pairs), True, errors
        step = f"pair {calls[i][0]} x {calls[j][0]}"
        progress.say(step)
        run_phase(lambda t, i=i, j=j: [calls[i]] if t % 2 == 0 else [calls[j]], "pair", step)
        driven += 1
        if on_pair is not None:
            on_pair(driven, len(pairs))

    if time.monotonic() <= deadline and calls:
        progress.say(f"mix {plan.name}")
        run_phase(lambda t: calls, "mix", f"mix {plan.name}")
    return driven, len(pairs), False, errors


def main(argv):
    with open(argv[1]) as fh:
        cfg = json.load(fh)
    progress = Progress(cfg.get("progress_path"))
    started = time.monotonic()
    deadline = started + float(cfg["budget_seconds"])
    seed = int(cfg["seed"])

    surface = Surface(cfg, progress)
    surface.load()
    only = set(cfg.get("only") or [])

    plans = []
    for qual, cls in surface.types:
        if only and not only.intersection(surface.keys(qual)):
            continue
        plans.append(plan_type(surface, qual, cls, cfg, progress))
    by_module = {}
    for qual, fn in surface.functions:
        by_module.setdefault(qual.rsplit(".", 1)[0], []).append((qual, fn))
    for module_name, fns in by_module.items():
        if only and not only.intersection(surface.keys(module_name)) and not any(
            only.intersection(surface.keys(q)) for q, _ in fns
        ):
            continue
        plans.append(plan_functions(surface, module_name, fns, cfg, progress))

    counters = Counters()
    groups = []
    # Mutators: Python callables run on their own threads throughout every
    # phase, changing shared state while the extension works on it — the
    # container-mutated-mid-serialisation class of bug needs a writer that is
    # plain Python, which the driven calls alone never provide.
    mutators = [(name, surface.evaluate(expr)) for name, expr in cfg.get("mutators", {}).items()]
    mutators += list(surface.py_mutators.items())
    if only and not plans:
        # A name that matches nothing must say so, with the names that exist,
        # not report "0 of 0 driven" (a first user typed the short class name).
        available = sorted({k for m, _ in surface.modules for k in surface.keys(m)})
        available += sorted({k for q, _ in surface.types for k in surface.keys(q)})
        result = {"seed": seed, "complete": True, "groups": [], "calls": {}, "returned": {},
                  "exceptions": {}, "messages": {}, "import_errors": surface.errors,
                  "only_unmatched": sorted(only), "available": available}
        with open(cfg["output_path"], "w") as fh:
            json.dump(result, fh, indent=2)
        progress.say("done")
        return 0
    entries = []
    for plan in plans:
        entry = {
            "name": plan.name,
            "kind": plan.kind,
            "factory": plan.factory,
            "driven": [q for q, _, _ in plan.calls],
            "arguments": {q: [_format_call(a) for a in argsets] for q, _, argsets in plan.calls},
            "not_driven": plan.not_driven,
            "serial_exceptions": plan.serial,
            "confined": plan.confined,
            "pairs_driven": 0,
            "pairs_total": 0,
            "budget_exhausted": False,
            "worker_errors": [],
            "started": False,
        }
        entries.append(entry)
    # Every planned group is in the result before any is driven, so a run that
    # ends early still counts the whole surface (a hung run once read
    # "186 of 225" when the surface was 533).
    groups.extend(entries)
    _write_result(cfg, seed, started, surface, groups, counters, complete=False)
    for index, plan in enumerate(plans):
        entry = entries[index]
        entry["started"] = True
        if plan.confined:
            entry["confined_callables"] = entry.pop("driven")
            entry["driven"] = []
        elif plan.make is not None and plan.calls:
            # Each group gets a fair share of what is left, so one slow group
            # cannot starve the rest (observed: whole groups never driven).
            remaining = sum(
                1 for p in plans[index:] if p.make is not None and p.calls and not p.confined
            )
            now = time.monotonic()
            group_deadline = now + max(deadline - now, 0) / max(remaining, 1)
            # Each group's schedule comes from the seed and its own name, so
            # `--replay SEED --only NAME` reproduces exactly what NAME saw in
            # the full run. (A string seeds random.Random stably.)
            group_rng = random.Random(f"{seed}:{plan.name}")
            # Registered before driving and refreshed as pairs finish: a crash
            # or hang mid-group must not erase what this group already drove
            # (it did, on three public projects).
            entry["in_progress"] = True
            _write_result(cfg, seed, started, surface, groups, counters, complete=False)
            last_write = [time.monotonic()]

            def on_pair(done, total, entry=entry, last_write=last_write):
                entry["pairs_driven"], entry["pairs_total"] = done, total
                now = time.monotonic()
                if now - last_write[0] > 2:
                    last_write[0] = now
                    _write_result(cfg, seed, started, surface, groups, counters, complete=False)

            def on_tick(last_write=last_write):
                # Counts saved while a pair runs, not only when it ends: a
                # crash in the first pair otherwise left the result with no
                # calls at all (seen validating mutators on a public project).
                now = time.monotonic()
                if now - last_write[0] > 5 or not last_write[1:]:
                    last_write[:] = [now, True]
                    _write_result(cfg, seed, started, surface, groups, counters, complete=False)

            driven, total, exhausted, errors = drive(
                plan, cfg, group_rng, counters, progress, group_deadline, on_pair, mutators, on_tick
            )
            entry["in_progress"] = False
            entry.update(
                pairs_driven=driven,
                pairs_total=total,
                budget_exhausted=exhausted,
                worker_errors=errors[:3],
            )
        # Written after every group: if a later group crashes the process,
        # what was already driven is still reported.
        _write_result(cfg, seed, started, surface, groups, counters, complete=False)

    _write_result(cfg, seed, started, surface, groups, counters, complete=True)
    progress.say("done")
    return 0


_WRITE_LOCK = threading.Lock()


def _write_result(cfg, seed, started, surface, groups, counters, complete):
    # Written from the main thread (per pair) and the heartbeat (per tick).
    with _WRITE_LOCK:
        _write_result_locked(cfg, seed, started, surface, groups, counters, complete)


def _write_result_locked(cfg, seed, started, surface, groups, counters, complete):
    with counters.lock:
        calls = dict(counters.calls)
        returned = dict(counters.returned)
        exceptions = {k: dict(v) for k, v in counters.exceptions.items()}
        messages = {k: dict(v) for k, v in counters.messages.items()}
        panics = {k: dict(v) for k, v in counters.panics.items()}
        panic_threads = {k: [list(r) for r in v] for k, v in counters.panic_threads.items()}
    for g in groups:
        g["raised_all"] = [q for q in g.get("driven", []) if calls.get(q) and not returned.get(q)]
    result = {
        "seed": seed,
        "complete": complete,
        "threads": cfg["threads"],
        "iterations": cfg["iterations"],
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "import_errors": surface.errors,
        "mutators": sorted(cfg.get("mutators", {})) + sorted(surface.py_mutators),
        "groups": groups,
        "calls": calls,
        "returned": returned,
        "exceptions": exceptions,
        "messages": messages,
        "panics": panics,
        "panic_threads": panic_threads,
    }
    tmp = cfg["output_path"] + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=2)
    # Atomic: a reader never sees half a file, even if the process dies here.
    os.replace(tmp, cfg["output_path"])


if __name__ == "__main__":
    sys.exit(main(sys.argv))

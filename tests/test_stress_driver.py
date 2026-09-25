# SPDX-License-Identifier: MIT OR Apache-2.0
"""The stress driver, run for real against a pure-Python stand-in module.

The driver is stdlib-only, so it runs here without ThreadSanitizer. What these
tests pin is everything except detection: surface enumeration, factory
precedence and derivation, argument derivation, skips, coverage reasons, and
that a seed reproduces the schedule exactly.
"""
import json
import subprocess
import sys
import textwrap

from ftcheck.stress import DRIVER

STANDIN = textwrap.dedent(
    '''
    import threading

    class Settings:
        def __init__(self, capacity):
            if capacity <= 0:
                raise ValueError("capacity must be positive")
            self.capacity = capacity

    class Counter:
        def __init__(self, start):
            if not isinstance(start, int):
                raise TypeError("start must be int")
            self.n = start
            self.lock = threading.Lock()
        def add(self, k):
            if not isinstance(k, int):
                raise TypeError("k must be int")
            with self.lock:
                self.n += k
        def get(self):
            return self.n
        def scaled(self, factor, *, offset=0):
            return self.n * factor + offset
        def reject(self, x):
            raise ValueError("always invalid")
        def close(self):
            raise AssertionError("close must never be driven when skipped")

    class NeedsSettings:
        def __init__(self, settings):
            if not isinstance(settings, Settings):
                raise TypeError("settings must be a Settings")
            self.capacity = settings.capacity
        def size(self):
            return self.capacity

    class Kind:
        """Shaped like a PyO3 enum: not constructible, variants as attributes."""
        def __init__(self, *a, **k):
            raise TypeError("cannot create 'Kind' instances")
        def label(self):
            return self._label

    for _name in ("FAST", "SLOW"):
        _v = object.__new__(Kind)
        _v._label = _name
        setattr(Kind, _name, _v)

    def ping():
        return 1

    class Boom(BaseException):
        """Shaped like pyo3_runtime.PanicException: not an Exception."""

    def panics():
        raise Boom("rust panicked")

    class Confined:
        """Shaped like #[pyclass(unsendable)]: raises off its creating thread."""
        def __init__(self):
            self.owner = threading.get_ident()
        def poke(self):
            if threading.get_ident() != self.owner:
                raise BaseException("standin::Confined is unsendable, but sent to another thread")
            return 1

    import types as _types
    shapes = _types.ModuleType("standin.shapes")  # a PyO3-style submodule: no __file__
    def _area(data):
        return bytes(data)
    shapes.area = _area

    def _private():
        raise AssertionError("private names are not surface")
    '''
)


def run_driver(tmp_path, expect_crash=False, **overrides):
    (tmp_path / "standin.py").write_text(STANDIN)
    cfg = {
        "modules": ["standin"],
        "factories": {},
        "args": {},
        "skip": {"standin.Counter.close": "invalidates the instance"},
        "factories_file": None,
        "seed": 7,
        "threads": 4,
        "iterations": 50,
        "budget_seconds": 60,
        "only": [],
        "progress_path": str(tmp_path / "progress.txt"),
        "output_path": str(tmp_path / "result.json"),
    }
    cfg.update(overrides)
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    proc = subprocess.run(
        [sys.executable, str(DRIVER), str(tmp_path / "cfg.json")],
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if not expect_crash:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((tmp_path / "result.json").read_text())


def group(result, name):
    (g,) = [g for g in result["groups"] if g["name"] == name]
    return g


def test_a_primitive_constructor_is_derived(tmp_path):
    g = group(run_driver(tmp_path), "standin.Counter")
    assert g["factory"] == "derived: Counter(1)"
    assert {"standin.Counter.add", "standin.Counter.get"} <= set(g["driven"])


def test_every_pair_is_driven_including_a_method_with_itself(tmp_path):
    g = group(run_driver(tmp_path), "standin.Counter")
    n = len(g["driven"])
    assert g["pairs_total"] == n * (n + 1) // 2, "every unordered pair, each with itself too"
    assert g["pairs_driven"] == g["pairs_total"]


def test_a_skip_is_honoured_and_reported_with_its_reason(tmp_path):
    g = group(run_driver(tmp_path), "standin.Counter")
    assert g["not_driven"]["standin.Counter.close"] == "skipped: invalidates the instance"


def test_an_underivable_constructor_is_named_not_silently_dropped(tmp_path):
    g = group(run_driver(tmp_path), "standin.NeedsSettings")
    assert g["factory"] is None
    assert g["pairs_driven"] == 0
    assert "no factory" in g["not_driven"]["standin.NeedsSettings.size"]
    assert "settings" in g["not_driven"]["standin.NeedsSettings.size"]


def test_a_declared_factory_unlocks_the_type(tmp_path):
    result = run_driver(
        tmp_path,
        factories={"standin.NeedsSettings": "standin.NeedsSettings(standin.Settings(4))"},
    )
    g = group(result, "standin.NeedsSettings")
    assert g["factory"].startswith("declared: ")
    assert g["driven"] == ["standin.NeedsSettings.size"]
    assert g["pairs_driven"] == 1


def test_declared_arguments_replace_derivation(tmp_path):
    result = run_driver(tmp_path, args={"standin.Counter.add": ["5", "7"]})
    assert group(result, "standin.Counter")["arguments"]["standin.Counter.add"] == ["(5)", "(7)"]


def test_module_functions_are_driven_as_a_group_and_private_names_ignored(tmp_path):
    g = group(run_driver(tmp_path), "standin (module functions)")
    assert "standin.ping" in g["driven"]
    assert all("_private" not in q for q in g["not_driven"])


def test_the_same_seed_reproduces_the_schedule_exactly(tmp_path):
    """Each thread's call sequence comes from the seed, not from the OS scheduler."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = run_driver(tmp_path / "a")
    b = run_driver(tmp_path / "b")
    assert a["calls"] == b["calls"]
    assert [g["arguments"] for g in a["groups"]] == [g["arguments"] for g in b["groups"]]


def test_a_different_seed_changes_the_schedule(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = run_driver(tmp_path / "a", seed=1)
    b = run_driver(tmp_path / "b", seed=2)
    assert a["calls"] != b["calls"]


def test_only_narrows_to_one_type(tmp_path):
    result = run_driver(tmp_path, only=["standin.Counter"])
    assert [g["name"] for g in result["groups"]] == ["standin.Counter"]


def test_an_exhausted_budget_is_reported_not_hidden(tmp_path):
    g = group(run_driver(tmp_path, budget_seconds=0.000001), "standin.Counter")
    assert g["budget_exhausted"] is True
    assert g["pairs_driven"] < g["pairs_total"]


def test_an_enum_like_type_is_derived_from_its_first_variant(tmp_path):
    g = group(run_driver(tmp_path), "standin.Kind")
    assert g["factory"] == "derived: Kind.FAST"
    assert g["driven"] == ["standin.Kind.label"], "variants are values, not callables"


def test_declared_arguments_may_be_keyword_arguments(tmp_path):
    result = run_driver(tmp_path, args={"standin.Counter.scaled": ["2, offset=5"]})
    g = group(result, "standin.Counter")
    assert g["arguments"]["standin.Counter.scaled"] == ["(2, offset=5)"]
    assert result["returned"]["standin.Counter.scaled"] > 0


def test_a_callable_that_raises_on_every_call_is_not_counted_as_driven(tmp_path):
    from ftcheck.stress import coverage

    result = run_driver(tmp_path)
    assert result["calls"]["standin.Counter.reject"] > 0
    assert result["returned"].get("standin.Counter.reject", 0) == 0
    driven, _total = coverage(result)
    names = [q for g in result["groups"] for q in g["driven"]]
    assert driven == len([q for q in names if result["returned"].get(q, 0) > 0])


def test_a_factories_file_may_define_dataclasses(tmp_path):
    (tmp_path / "factories.py").write_text(
        "import dataclasses, standin\n"
        "@dataclasses.dataclass\n"
        "class Spec:\n"
        "    start: int = 3\n"
        "FACTORIES = {'standin.Counter': lambda: standin.Counter(Spec().start)}\n"
        "ARGS = {'standin.Counter.scaled': [((2,), {'offset': 1})]}\n"
    )
    result = run_driver(tmp_path, factories_file=str(tmp_path / "factories.py"))
    g = group(result, "standin.Counter")
    assert g["factory"].startswith("declared in")
    assert g["arguments"]["standin.Counter.scaled"] == ["(2, offset=1)"]


def test_worker_threads_do_not_share_the_main_threads_context(tmp_path):
    """3.14t threads inherit a copy of the parent's context, whose values are
    shared objects; the driver gives each worker an empty one instead."""
    import inspect
    import threading

    if "context" not in inspect.signature(threading.Thread).parameters:
        import pytest

        pytest.skip("Thread(context=) needs Python 3.14")
    (tmp_path / "ctxmod.py").write_text(
        "import contextvars, threading\n"
        "VAR = contextvars.ContextVar('v', default=None)\n"
        "VAR.set('main')\n"
        "SEEN = set()\n"
        "def peek():\n"
        "    if threading.current_thread().name.startswith('ftw-'):\n"
        "        SEEN.add(VAR.get())\n"
        "        if VAR.get() == 'main':\n"
        "            raise RuntimeError('inherited the main context')\n"
    )
    result = run_driver(tmp_path, modules=["ctxmod"], skip={})
    assert "RuntimeError" not in str(result["exceptions"])


def test_a_panic_does_not_end_the_run(tmp_path):
    """PyO3's PanicException derives from BaseException; it is counted, not fatal."""
    result = run_driver(tmp_path)
    assert result["exceptions"]["standin.panics"] == {"Boom": result["calls"]["standin.panics"]}


def test_native_submodules_are_part_of_the_surface(tmp_path):
    result = run_driver(tmp_path)
    names = [g["name"] for g in result["groups"]]
    assert "standin.shapes (module functions)" in names
    assert group(result, "standin.shapes (module functions)")["driven"] == ["standin.shapes.area"]


def test_an_unsendable_type_is_reported_confined_not_as_a_hole(tmp_path):
    from ftcheck.stress import confined, coverage

    result = run_driver(tmp_path, only=["standin.Confined"])
    g = group(result, "standin.Confined")
    assert g["confined"].startswith("thread-confined")
    assert g["confined_callables"] == ["standin.Confined.poke"]
    assert g["pairs_driven"] == 0
    assert coverage(result) == (0, 0)
    assert confined(result) == 1


def test_replaying_one_type_reproduces_its_schedule_from_the_full_run(tmp_path):
    """`--replay SEED --only NAME` must give NAME the schedule it had in the full
    run; with one RNG shared across types, the earlier types shifted it."""
    (tmp_path / "full").mkdir()
    (tmp_path / "only").mkdir()
    full = run_driver(tmp_path / "full", seed=11)
    only = run_driver(tmp_path / "only", seed=11, only=["standin.Counter"])
    counter = [q for q in full["calls"] if q.startswith("standin.Counter.")]
    assert {q: full["calls"][q] for q in counter} == {q: only["calls"][q] for q in counter}


def test_results_are_marked_complete_and_written_atomically(tmp_path):
    result = run_driver(tmp_path)
    assert result["complete"] is True
    assert not (tmp_path / "result.json.tmp").exists()



def test_a_type_with_no_python_constructor_gets_a_clear_reason(tmp_path):
    g = group(run_driver(tmp_path), "standin.Kind")
    assert g["factory"].startswith("derived"), "enums still come from their variants"
    (tmp_path / "noctor").mkdir()
    (tmp_path / "noctor" / "noctor.py").write_text(
        "class Handle:\n"
        "    def __init__(self, *a, **k):\n"
        "        raise TypeError(\"cannot create 'builtins.Handle' instances\")\n"
        "    def read(self):\n"
        "        return 1\n"
    )
    result = run_driver(tmp_path / "noctor", modules=["noctor"], skip={})
    reason = group(result, "noctor.Handle")["not_driven"]["noctor.Handle.read"]
    assert reason.startswith("no Python constructor")



def test_a_crash_mid_group_keeps_what_was_driven(tmp_path):
    """Regression: a crash or kill mid-group erased the
    group, and coverage read 0 of 0."""
    (tmp_path / "crashy.py").write_text(
        "import os, threading\n"
        "CALLS = [0]\n"
        "LOCK = threading.Lock()\n"
        "def fine():\n"
        "    return 1\n"
        "def dies():\n"
        "    if not threading.current_thread().name.startswith(('ftw-', 'ftc-')):\n"
        "        return 1  # derivation and baseline run on the main thread\n"
        "    with LOCK:\n"
        "        CALLS[0] += 1\n"
        "        if CALLS[0] > 150:\n"
        "            os._exit(9)\n"
    )
    result = run_driver(tmp_path, expect_crash=True, modules=["crashy"], skip={})
    assert result["complete"] is False
    (g,) = [g for g in result["groups"] if g["name"] == "crashy (module functions)"]
    assert g["in_progress"] is True
    assert sorted(g["driven"]) == ["crashy.dies", "crashy.fine"]



def test_mutators_run_throughout_the_drive_phase(tmp_path):
    """A plain-Python writer alongside the driven calls: what the
    container-mutated-mid-call bugs need."""
    (tmp_path / "mutated.py").write_text(
        "SHARED = list(range(100))\n"
        "def total():\n"
        "    return sum(list(SHARED))\n"
    )
    (tmp_path / "factories.py").write_text(
        "import mutated\n"
        "def grow_and_shrink():\n"
        "    mutated.SHARED.append(1)\n"
        "    mutated.SHARED.pop()\n"
        "MUTATORS = {'churn': grow_and_shrink}\n"
    )
    result = run_driver(
        tmp_path, modules=["mutated"], skip={}, factories_file=str(tmp_path / "factories.py")
    )
    assert result["mutators"] == ["churn"]
    assert result["calls"]["mutator:churn"] > 0
    assert result["calls"]["mutated.total"] > 0


def test_the_budget_is_honoured_per_call(tmp_path):
    """One ever-slower callable must not hold a phase past the budget."""
    import time

    (tmp_path / "slow.py").write_text(
        "import time\n"
        "def crawl():\n"
        "    time.sleep(0.05)\n"
    )
    started = time.monotonic()
    run_driver(tmp_path, modules=["slow"], skip={}, budget_seconds=1, iterations=10_000)
    assert time.monotonic() - started < 20


def test_inherited_methods_from_an_unexported_base_are_driven(tmp_path):
    """Regression (public project): #[pyclass(extends=Base)] with an unexported
    base showed no callables and "no race" without calling the main API."""
    (tmp_path / "derived.py").write_text(
        "class _Base:\n"
        "    def is_valid(self, x):\n"
        "        return isinstance(x, int)\n"
        "class Draft(_Base):\n"
        "    pass\n"
    )
    g = group(run_driver(tmp_path, modules=["derived"], skip={}), "derived.Draft")
    assert "derived.Draft.is_valid" in g["driven"]


def test_a_panic_on_the_second_sequential_call_belongs_to_the_baseline(tmp_path):
    """Regression: a method that panics on its second call on one thread (a
    duplicate insert, say) read as concurrency-only when each call got a
    fresh instance."""
    (tmp_path / "dup.py").write_text(
        "class Boom(BaseException):\n"
        "    pass\n"
        "class Builder:\n"
        "    def __init__(self):\n"
        "        self.fields = set()\n"
        "    def add(self, name):\n"
        "        if name in self.fields:\n"
        "            raise Boom('Field already exists')\n"
        "        self.fields.add(name)\n"
    )
    g = group(run_driver(tmp_path, modules=["dup"], skip={}), "dup.Builder")
    assert "Boom" in g["serial_exceptions"]["dup.Builder.add"]


def test_groups_never_started_still_count_toward_the_surface(tmp_path):
    from ftcheck.stress import coverage

    (tmp_path / "early.py").write_text(
        "import os, threading\n"
        "class A:\n"
        "    def dies(self):\n"
        "        if threading.current_thread().name.startswith(('ftw-pair', 'ftw-mix')):\n"
        "            os._exit(9)\n"
        "class B:\n"
        "    def one(self):\n"
        "        return 1\n"
        "    def two(self):\n"
        "        return 2\n"
    )
    result = run_driver(tmp_path, expect_crash=True, modules=["early"], skip={})
    names = [g["name"] for g in result["groups"]]
    assert "early.B" in names, "a group never reached is still part of the surface"
    assert coverage(result)[1] >= 3


def test_property_setters_are_driven_as_their_own_callable(tmp_path):
    """Regression (public project): every field had get and set; only getters
    were driven while coverage read 100%."""
    (tmp_path / "fields.py").write_text(
        "class Duration:\n"
        "    def __init__(self):\n"
        "        self._days = 1\n"
        "    @property\n"
        "    def days(self):\n"
        "        return self._days\n"
        "    @days.setter\n"
        "    def days(self, v):\n"
        "        self._days = v\n"
        "    @property\n"
        "    def frozen(self):\n"
        "        return 0\n"
    )
    g = group(run_driver(tmp_path, modules=["fields"], skip={}), "fields.Duration")
    assert "fields.Duration.days (set)" in g["driven"]
    assert "fields.Duration.frozen (set)" not in g["driven"], "read-only stays read-only"



def test_a_private_method_is_driven_when_its_arguments_are_declared(tmp_path):
    (tmp_path / "priv.py").write_text(
        "class Enc:\n"
        "    def _encode_bytes(self, b):\n"
        "        return len(b)\n"
    )
    result = run_driver(
        tmp_path, modules=["priv"], skip={}, args={"priv.Enc._encode_bytes": ["b'abc'"]}
    )
    assert "priv.Enc._encode_bytes" in group(result, "priv.Enc")["driven"]


def test_mutator_threads_keep_their_name_within_fifteen_characters(tmp_path):
    (tmp_path / "names.py").write_text(
        "import threading\n"
        "SEEN = set()\n"
        "def ping():\n"
        "    return 1\n"
    )
    (tmp_path / "factories.py").write_text(
        "import threading, names\n"
        "def record():\n"
        "    names.SEEN.add(threading.current_thread().name)\n"
        "    if len(threading.current_thread().name) > 15:\n"
        "        raise AssertionError('name too long for the kernel')\n"
        "MUTATORS = {'churn_the_dict': record}\n"
    )
    result = run_driver(
        tmp_path, modules=["names"], skip={}, factories_file=str(tmp_path / "factories.py")
    )
    assert "AssertionError" not in str(result["exceptions"])
    assert result["calls"]["mutator:churn_the_dict"] > 0


def test_json_groups_list_what_raised_on_every_call(tmp_path):
    g = group(run_driver(tmp_path), "standin.Counter")
    assert "standin.Counter.reject" in g["raised_all"]

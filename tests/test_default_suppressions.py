# SPDX-License-Identifier: MIT OR Apache-2.0
"""ftcheck's default TSan suppressions: what they hide, and what they must not.

TSan's matching is reproduced here closely enough to check each entry against
synthetic reports shaped like the ones TSan prints. A `race_top:` entry is
checked against the top frame of each access; a `race:` entry against every
frame of every access and of every thread-creation stack. Either way, one
matching stack suppresses the whole report.

The sanitizer ground truth (`clean/clean-dependency-fences`,
`racy/race-in-dependency-callbacks`) proves the same thing under a real TSan.
"""
import re

from ftcheck.ci.pipeline import DEFAULT_SUPPRESSIONS

# `race:` matches any frame, so an entry that names a function which calls back
# into other code would hide races in that code too. Each one here was checked
# against the dependency's source: the function it names runs none of the
# caller's code. Adding a `race:` entry means adding it here, deliberately.
REVIEWED_WHOLE_STACK_ENTRIES = {
    "race:oneshot::*::write_message::",
    "race:drop<oneshot::Channel<",
    "race:drop<crossbeam_epoch::internal::Local, alloc::alloc::Global>",
    "race:_dl_deallocate_tls",
}


def _entries():
    lines = DEFAULT_SUPPRESSIONS.read_text().splitlines()
    return [(i, ln) for i, ln in enumerate(lines) if ln.strip() and not ln.startswith("#")], lines


def _matches(template: str, text: str) -> bool:
    """sanitizer_common's TemplateMatch: substring, `*` wildcard, `^`/`$` anchors."""
    if not text:
        return False
    start = template.startswith("^")
    end = template.endswith("$")
    body = template[start: len(template) - end if end else None]
    pattern = ".*".join(re.escape(part) for part in body.split("*"))
    return re.search(("^" if start else "") + pattern + ("$" if end else ""), text) is not None


def _suppressed(accesses, threads=()):
    """accesses: stacks of the two accesses, top frame first; frames are function names."""
    entries = [ln for _, ln in _entries()[0]]
    for entry in entries:
        kind, _, template = entry.partition(":")
        if kind == "race_top":
            frames = [stack[0] for stack in accesses if stack]
        else:
            frames = [f for stack in (*accesses, *threads) for f in stack]
        if any(_matches(template, f) for f in frames):
            return entry
    return None


def test_every_default_suppression_is_narrow_and_justified():
    """`race_top:` by default; `race:` only when reviewed. Each preceded by a comment."""
    entries, lines = _entries()
    assert entries, "the default file must not be empty"
    for i, line in entries:
        assert line.startswith(("race_top:", "race:")), line
        if line.startswith("race:"):
            assert line in REVIEWED_WHOLE_STACK_ENTRIES, (
                f"{line}: a `race:` entry matches every frame; review it and list it here"
            )
        assert any(lines[j].startswith("#") for j in range(max(0, i - 3), i)), line


def test_the_matcher_follows_tsan():
    assert _matches("drop<oneshot::Channel<", "drop<oneshot::Channel<[u64; 4]>, alloc::alloc::Global>")
    assert _matches("oneshot::*::write_message::", "f<T, oneshot::{impl#9}::write_message::{closure_env#0}<T>>")
    assert not _matches("^write_message", "oneshot::write_message")
    assert _matches("free$", "free") and not _matches("free$", "freed")


# --- what the entries hide: the dependencies' own reports ----------------------

_USER = ["fixture::caller", "<fixture::Api>::__pymethod_call__"]

_ONESHOT_WRITE = [
    "__tsan_memcpy",
    "write<[u64; 32]>",
    "{closure#0}<[u64; 32]>",
    "with_message_mut<[u64; 32], oneshot::{impl#9}::write_message::{closure_env#0}<[u64; 32]>>",
    "write_message<[u64; 32]>",
    "<oneshot::Sender<[u64; 32]>>::send",
    "{closure#0}",
]
_ONESHOT_TAKE = [
    "__tsan_memcpy",
    "read<core::mem::maybe_uninit::MaybeUninit<[u64; 32]>>",
    "take_message<[u64; 32]>",
    "<oneshot::Receiver<[u64; 32]>>::try_recv",
    *_USER,
]
_ONESHOT_FREE = [
    "free",
    "dealloc",
    "__rustc::__rdl_dealloc",
    "dealloc",
    "drop<oneshot::Channel<[u64; 32]>, alloc::alloc::Global>",
    "drop_in_place<alloc::boxed::Box<oneshot::Channel<[u64; 32]>, alloc::alloc::Global>>",
    "dealloc<[u64; 32]>",
    "<oneshot::Receiver<[u64; 32]>>::recv",
    *_USER,
]
_BAG_READ = [
    "read<crossbeam_epoch::internal::SealedBag>",
    "assume_init_read<crossbeam_epoch::internal::SealedBag>",
    "<crossbeam_epoch::sync::queue::Queue<crossbeam_epoch::internal::SealedBag>>::try_pop_if",
    "<crossbeam_epoch::internal::Global>::collect",
]
_NODE_FREE = [
    "free",
    "dealloc",
    "drop<crossbeam_epoch::sync::queue::Node<crossbeam_epoch::internal::SealedBag>, alloc::alloc::Global>",
    "<crossbeam_epoch::internal::Global>::collect",
]
_LOCAL_LOAD = [
    "atomic_load<usize>",
    "load",
    "load<crossbeam_epoch::sync::list::Entry>",
    "next<crossbeam_epoch::internal::Local, crossbeam_epoch::internal::Local>",
    "<crossbeam_epoch::internal::Global>::try_advance",
]
_LOCAL_FREE = [
    "free",
    "dealloc",
    "drop<crossbeam_epoch::internal::Local, alloc::alloc::Global>",
    "drop_in_place<alloc::boxed::Box<crossbeam_epoch::internal::Local, alloc::alloc::Global>>",
    "<crossbeam_epoch::internal::Local as crossbeam_epoch::atomic::Pointable>::drop",
    "<crossbeam_epoch::internal::Global>::collect",
]
_TLS_FREE = [
    "free",
    "_dl_deallocate_tls",
    "<std::sys::thread::unix::Thread as core::ops::drop::Drop>::drop",
    "drop_in_place<std::sys::thread::unix::Thread>",
    "drop_in_place<std::thread::lifecycle::JoinInner<()>>",
    "core::ptr::drop_in_place::<std::thread::join_handle::JoinHandle<()>>",
    *_USER,
]
_TLS_OWN_ACCESS = [
    "replace<isize>",
    "try_borrow_mut<alloc::vec::Vec<u64, alloc::alloc::Global>>",
    "{closure#0}",
    "with<core::cell::RefCell<alloc::vec::Vec<u64, alloc::alloc::Global>>, fixture::worker::{closure_env#0}>",
]
_TLS_DTOR_LIST = [
    "replace<isize>",
    "try_borrow_mut<alloc::vec::Vec<(*mut u8, unsafe extern \"C\" fn(*mut u8))>>",
    "run",
    "std::sys::thread_local::guard::key::enable::run",
]


def test_the_crossbeam_deque_buffer_race_is_suppressed_at_its_top_frame_only():
    slot = "write_volatile<core::mem::maybe_uninit::MaybeUninit<rayon_core::job::JobRef>>"
    assert _suppressed([[slot, "<crossbeam_deque::deque::Buffer<rayon_core::job::JobRef>>::write"], _USER])
    assert _suppressed([["<fixture::Grid>::fill", slot], ["<fixture::Grid>::fill"]]) is None


def test_oneshot_message_handover_is_suppressed():
    assert _suppressed([_ONESHOT_TAKE, _ONESHOT_WRITE])
    assert _suppressed([_ONESHOT_FREE, _ONESHOT_WRITE])


def test_crossbeam_epoch_reclamation_is_suppressed():
    assert _suppressed([_NODE_FREE, _BAG_READ])
    assert _suppressed([_LOCAL_FREE, _LOCAL_LOAD])


def test_glibc_tls_teardown_is_suppressed():
    assert _suppressed([_TLS_FREE, _TLS_DTOR_LIST])
    assert _suppressed([_TLS_FREE, _TLS_OWN_ACCESS])


# --- what they must not hide: your code, run by the dependency -----------------

_RECEIPT_DROP = [
    "<fixture::Receipt as core::ops::drop::Drop>::drop",
    "drop_in_place<fixture::Receipt>",
    "assume_init_drop<fixture::Receipt>",
    "{closure#0}<fixture::Receipt>",
    "with_message_mut<fixture::Receipt, oneshot::{impl#9}::drop_message::{closure_env#0}<fixture::Receipt>>",
    "drop_message<fixture::Receipt>",
    "<oneshot::Receiver<fixture::Receipt> as core::ops::drop::Drop>::drop",
    "drop_in_place<oneshot::Receiver<fixture::Receipt>>",
    *_USER,
]
_DEFERRED_CLOSURE = [
    "{closure#0}",
    "<crossbeam_epoch::deferred::Deferred>::new::call::<fixture::deferred::{closure#0}>",
    "call",
    "drop",
    "drop_in_place<crossbeam_epoch::internal::Bag>",
    "drop_in_place<crossbeam_epoch::internal::SealedBag>",
    "drop<crossbeam_epoch::internal::SealedBag>",
    "<crossbeam_epoch::internal::Global>::collect",
    "<crossbeam_epoch::internal::Local>::flush",
    *_USER,
]
_BAG_DROP_IN_LOCAL = [
    "{closure#0}",
    "<crossbeam_epoch::deferred::Deferred>::new::call::<fixture::deferred::{closure#0}>",
    "drop_in_place<crossbeam_epoch::internal::Bag>",
    "drop_in_place<crossbeam_epoch::internal::Local>",
    "drop_in_place<alloc::boxed::Box<crossbeam_epoch::internal::Local, alloc::alloc::Global>>",
    "<crossbeam_epoch::internal::Local as crossbeam_epoch::atomic::Pointable>::drop",
]
_TLS_USER_DTOR = [
    "<fixture::Retire as core::ops::drop::Drop>::drop",
    "drop_in_place<fixture::Retire>",
    "destroy<fixture::Retire>",
    "run",
    "std::sys::thread_local::guard::key::enable::run",
]


def test_a_race_in_a_message_dropped_by_oneshot_is_still_reported():
    assert _suppressed([_RECEIPT_DROP, _RECEIPT_DROP]) is None


def test_a_race_in_a_closure_run_by_the_epoch_collector_is_still_reported():
    assert _suppressed([_DEFERRED_CLOSURE, _DEFERRED_CLOSURE]) is None
    assert _suppressed([_BAG_DROP_IN_LOCAL, _DEFERRED_CLOSURE]) is None


def test_a_race_in_a_thread_local_destructor_is_still_reported():
    assert _suppressed([_TLS_USER_DTOR, _TLS_USER_DTOR]) is None


def test_a_race_merely_on_a_thread_that_uses_the_dependency_is_still_reported():
    """Thread-creation stacks are matched by `race:` too; your callbacks in them are not."""
    mine = ["<fixture::Store>::put", *_USER]
    assert _suppressed([mine, mine], threads=[_RECEIPT_DROP, _DEFERRED_CLOSURE]) is None

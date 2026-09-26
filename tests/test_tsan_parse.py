# SPDX-License-Identifier: MIT OR Apache-2.0
"""TSan report parsing, against real reports captured from the pipeline.

The logs in tests/data/tsan/ are unedited output of ThreadSanitizer running
fixtures under ghcr.io/nascheme/cpython-tsan:3.14t with nightly-2026-01-15
(LLVM 21 on both sides). The paths in them are the container's.
"""
import pathlib

from ftcheck.ci.tsan import EXTERNAL, REACHED, YOURS, attribute, fatal_errors, parse, to_findings

DATA = pathlib.Path(__file__).parent / "data" / "tsan"
CRATE = "/tmp/c"


def load(name):
    return (DATA / name).read_text()


def test_parses_one_report_per_warning():
    reports = parse(load("raw-pointer.log"))
    assert len(reports) == 1
    assert reports[0].kind == "data race"


def test_context_sections_are_not_access_stacks():
    """'Location is heap block' and 'Thread T3 created by' describe context."""
    report = parse(load("raw-pointer.log"))[0]
    headers = [s.header for s in report.access_stacks]
    assert len(headers) == 2
    assert headers[0].startswith("Write of size 8")
    assert headers[1].startswith("Previous write of size 8")


def test_frames_carry_symbol_file_line_and_module():
    frame = parse(load("raw-pointer.log"))[0].access_stacks[0].frames[0]
    assert frame.symbol == "bump"
    assert frame.file == "/tmp/c/src/lib.rs"
    assert (frame.line, frame.column) == (35, 13)
    assert frame.module == "ft001_raw_pointer.cpython-314t-x86_64-linux-gnu.so"


def test_a_symbol_containing_spaces_survives():
    frames = parse(load("raw-pointer.log"))[0].access_stacks[0].frames
    assert frames[1].symbol == "<ft001_raw_pointer::Buffer>::__pymethod_bump__"


def test_an_unsymbolised_location_is_none_not_a_bogus_path():
    frames = parse(load("c-dependency.log"))[0].access_stacks[0].frames
    assert frames[0].symbol == "vsnprintf"
    assert frames[0].file is None


def test_attribution_looks_past_an_interceptor_top_frame():
    """The top frame is `vsnprintf` in the interpreter; the bug is the user's."""
    report = parse(load("c-dependency.log"))[0]
    assert report.access_stacks[0].frames[0].module.startswith("python3")
    assert attribute(report, {"ft005_unsync_c_dep.cpython-314t-x86_64-linux-gnu.so"}) == YOURS


def test_a_race_outside_the_users_modules_is_not_attributed_to_them():
    report = parse(load("c-dependency.log"))[0]
    assert attribute(report, {"someone_else.cpython-314t-x86_64-linux-gnu.so"}) == EXTERNAL


def test_findings_point_at_the_crates_own_source():
    mods = {"ft005_unsync_c_dep.cpython-314t-x86_64-linux-gnu.so"}
    mine, _, theirs = to_findings(parse(load("c-dependency.log")), mods, CRATE)
    assert theirs == []
    (finding,) = mine
    assert finding["rule"] == "tsan/data-race"
    assert finding["producer"] == "tsan"
    assert finding["confidence"] == "certain"
    assert finding["primary"] == {"file": "src/legacy.c", "line": 8, "column": 5}
    assert finding["symbol"] == "legacy_format"


def test_stored_stacks_start_at_the_users_frame():
    mods = {"ft005_unsync_c_dep.cpython-314t-x86_64-linux-gnu.so"}
    (finding,), _, _ = to_findings(parse(load("c-dependency.log")), mods, CRATE)
    tops = sorted(s["frames"][0]["symbol"] for s in finding["stacks"])
    assert "vsnprintf" not in tops, "the interceptor is noise; the message still names it"
    assert tops == ["format_value", "legacy_format"], (
        "std is compiled into the module under -Zbuild-std; a stack must open on the "
        "crate's own frame, not on core::ptr::copy_nonoverlapping"
    )
    for stack in finding["stacks"]:
        assert stack["frames"][0]["location"]["file"].startswith("src/")


def test_the_same_race_from_both_sides_is_one_finding():
    mods = {"ft001_raw_pointer.cpython-314t-x86_64-linux-gnu.so"}
    reports = parse(load("raw-pointer.log"))
    mine, _, _ = to_findings(reports + reports, mods, CRATE)
    assert len(mine) == 1
    assert mine[0]["occurrences"] == 2


def test_without_debuginfo_the_module_still_attributes_the_race():
    """Release builds without line tables lose file:line, not ownership."""
    mods = {"ft001_static_mut.cpython-314t-x86_64-linux-gnu.so"}
    mine, _, theirs = to_findings(parse(load("no-debuginfo.log")), mods, CRATE)
    assert theirs == []
    assert len(mine) == 1
    assert mine[0]["symbol"] == "ft001_static_mut::__pyfunction_bump"
    assert mine[0]["primary"]["line"] >= 1


def test_unattributed_reports_are_kept_separately_not_dropped():
    mine, _, theirs = to_findings(parse(load("raw-pointer.log")), {"other.so"}, CRATE)
    assert mine == []
    assert len(theirs) == 1


def test_fatal_startup_errors_are_detected():
    text = (
        "FATAL: ThreadSanitizer: encountered an incompatible memory layout but was "
        "unable to disable ASLR (perhaps sandboxing is enabled?).\n"
        "FATAL: ThreadSanitizer: encountered an incompatible memory layout but was "
        "unable to disable ASLR (perhaps sandboxing is enabled?).\n"
    )
    assert len(fatal_errors(text)) == 1


def test_a_clean_log_has_no_reports_and_no_fatal_errors():
    assert parse("collected 3 items\n3 passed\n") == []
    assert fatal_errors("3 passed\n") == []


def test_a_tsan_finding_renders_through_the_same_sarif_and_junit_emitters():
    """One SARIF file carries lint and sanitizer findings alike."""
    import json

    from ftcheck import _ftcheck

    mods = {"ft005_unsync_c_dep.cpython-314t-x86_64-linux-gnu.so"}
    mine, _, _ = to_findings(parse(load("c-dependency.log")), mods, CRATE)
    doc = json.loads(_ftcheck.to_sarif(json.dumps(mine), "0.0.0"))
    (result,) = doc["runs"][0]["results"]
    assert result["ruleId"] == "tsan/data-race"
    assert result["properties"]["producer"] == "tsan"
    assert len(result["relatedLocations"]) == 2
    (rule,) = doc["runs"][0]["tool"]["driver"]["rules"]
    assert rule["shortDescription"]["text"] == "Data race observed by ThreadSanitizer"
    assert 'failures="1"' in _ftcheck.to_junit(json.dumps(mine))


EXAMPLE = {"examplelib.cpython-314t-x86_64-linux-gnu.so"}


def test_a_race_inside_cpython_reached_from_the_extension_is_not_blamed_on_it():
    """Regression: a PyO3 method called decimal.Decimal() from many threads;
    both writes were in CPython's _decimal (the threads shared an inherited
    context). The extension's frames were only further down."""
    report = parse(load("cpython-reached-from-extension.log"))[0]
    assert attribute(report, EXAMPLE) == REACHED
    mine, reached, theirs = to_findings([report], EXAMPLE, CRATE)
    assert mine == [] and theirs == []
    (finding,) = reached
    assert finding["confidence"] == "likely"
    assert finding["symbol"] == "dec_addstatus", "shown from where the access happened"
    assert finding["message"].startswith("Inside CPython or another library")


def test_a_crash_with_no_stack_is_still_a_finding():
    """Regression: TSan printed a SEGV header, no stack, then hung. A process
    that died while being driven must never read as clean."""
    (report,) = parse(load("crash-no-stack.log"))
    assert report.kind == "SEGV" and report.is_crash
    mine, reached, theirs = to_findings([report], EXAMPLE, CRATE)
    (finding,) = mine
    assert finding["rule"] == "tsan/segv"
    assert "no stack" in finding["message"]
    assert reached == [] and theirs == []


def test_a_crash_with_the_extension_on_the_stack_is_yours():
    text = (
        "==9==ERROR: ThreadSanitizer: SEGV on unknown address 0x000000000028 (pc 0x1 bp 0x0 sp 0x2 T9)\n"
        "    #0 PyDict_Next /cpython/Objects/dictobject.c:3058:30 (libpython3.14t.so.1.0+0x1)\n"
        "    #1 walk /tmp/c/src/lib.rs:44:9 (examplelib.cpython-314t-x86_64-linux-gnu.so+0x2)\n"
        "SUMMARY: ThreadSanitizer: SEGV in PyDict_Next\n"
    )
    (report,) = parse(text)
    assert attribute(report, EXAMPLE) == YOURS, "a crash under your call is yours to look at"


def test_a_crash_entirely_outside_the_extension_is_not_yours():
    text = (
        "==9==ERROR: ThreadSanitizer: SEGV on unknown address 0x0 (pc 0x1 bp 0x0 sp 0x2 T9)\n"
        "    #0 PyDict_Next /cpython/Objects/dictobject.c:3058:30 (libpython3.14t.so.1.0+0x1)\n"
    )
    (report,) = parse(text)
    assert attribute(report, EXAMPLE) == EXTERNAL


def test_a_codegen_unit_is_not_mistaken_for_a_source_file():
    """Without line tables the symbolizer names `crate.hash-cgu.0`; taken as a
    relative path it beat real frames to the primary location."""
    frame = parse(load("no-debuginfo.log"))[0].access_stacks[0].frames[0]
    assert frame.file is None



def test_an_extension_side_racing_plain_python_inside_cpython_is_yours():
    """The extension iterates a dict with PyDict_Next, no critical section,
    while plain Python code pops from it.
    Both accesses are in CPython, but only one side came through the
    extension: that is the extension's bug, not a CPython-internal race."""
    text = (
        "WARNING: ThreadSanitizer: data race (pid=1)\n"
        "  Read of size 8 at 0x10 by thread T1:\n"
        "    #0 _PyDict_Next /cpython/Objects/dictobject.c:3058:30 (libpython3.14t.so.1.0+0x1)\n"
        "    #1 PyDict_Next /cpython/Objects/dictobject.c:3099:12 (libpython3.14t.so.1.0+0x2)\n"
        "    #2 next /tmp/c/src/walk/mapping.rs:12:9 (examplelib.cpython-314t-x86_64-linux-gnu.so+0x3)\n"
        "  Previous write of size 8 at 0x10 by thread T2:\n"
        "    #0 delitem_common /cpython/Objects/dictobject.c:2500:5 (libpython3.14t.so.1.0+0x4)\n"
        "    #1 dict_pop /cpython/Objects/dictobject.c:4400:5 (libpython3.14t.so.1.0+0x5)\n"
        "SUMMARY: ThreadSanitizer: data race in _PyDict_Next\n"
    )
    (report,) = parse(text)
    assert attribute(report, EXAMPLE) == YOURS
    (finding,), reached, _ = to_findings([report], EXAMPLE, CRATE)
    assert reached == []
    tops = sorted(s["frames"][0]["symbol"] for s in finding["stacks"])
    assert tops == ["_PyDict_Next", "delitem_common"], "each side shown where it happened"
    assert finding["primary"]["file"] == "src/walk/mapping.rs", "but pointing at your line"
    assert finding["primary"]["line"] == 12



def test_a_python_callback_the_extension_invoked_is_still_python_code():
    """The writer is a dict.pop() in a Python callback the extension has
    invoked, so extension frames sit below the
    interpreter's eval loop. That side is Python code; the race is the
    extension's, not CPython-internal."""
    text = (
        "WARNING: ThreadSanitizer: data race (pid=1)\n"
        "  Read of size 8 at 0x10 by thread T1:\n"
        "    #0 _PyDict_Next /cpython/Objects/dictobject.c:3058:30 (libpython3.14t.so.1.0+0x1)\n"
        "    #1 next /tmp/c/src/walk/mapping.rs:12:9 (examplelib.cpython-314t-x86_64-linux-gnu.so+0x3)\n"
        "  Previous write of size 8 at 0x10 by thread T2:\n"
        "    #0 delitem_common /cpython/Objects/dictobject.c:2500:5 (libpython3.14t.so.1.0+0x4)\n"
        "    #1 _PyEval_EvalFrameDefault /cpython/Python/generated_cases.c.h:1621:35 (libpython3.14t.so.1.0+0x5)\n"
        "    #2 encode_value /tmp/c/src/encode/value.rs:88:21 (examplelib.cpython-314t-x86_64-linux-gnu.so+0x6)\n"
        "SUMMARY: ThreadSanitizer: data race in _PyDict_Next\n"
    )
    (report,) = parse(text)
    assert attribute(report, EXAMPLE) == YOURS



def test_a_thread_safe_c_api_racing_inside_cpython_is_not_blamed_on_the_extension():
    """PyList_GetItemRef — documented thread-safe — racing a list.pop()
    memmove inside CPython. The extension uses the API
    correctly; the report is CPython's."""
    text = (
        "WARNING: ThreadSanitizer: data race (pid=1)\n"
        "  Read of size 8 at 0x10 by thread T1:\n"
        "    #0 list_get_item_ref /cpython/Objects/listobject.c:340:12 (libpython3.14t.so.1.0+0x1)\n"
        "    #1 PyList_GetItemRef /cpython/Objects/listobject.c:380:12 (libpython3.14t.so.1.0+0x2)\n"
        "    #2 extract /tmp/c/src/convert.rs:120:9 (examplelib.cpython-314t-x86_64-linux-gnu.so+0x3)\n"
        "  Previous write of size 8 at 0x10 by thread T2:\n"
        "    #0 __tsan_memmove <null> (python3.14+0x9)\n"
        "    #1 list_pop_impl /cpython/Objects/listobject.c:1559:5 (libpython3.14t.so.1.0+0x4)\n"
        "    #2 _PyEval_EvalFrameDefault /cpython/Python/generated_cases.c.h:1621:35 (libpython3.14t.so.1.0+0x5)\n"
        "SUMMARY: ThreadSanitizer: data race in list_get_item_ref\n"
    )
    (report,) = parse(text)
    assert attribute(report, EXAMPLE) == REACHED


def _race(read_frames, write_frames, writer_name=None):
    lines = ["WARNING: ThreadSanitizer: data race (pid=1)", "  Read of size 8 at 0x10 by thread T1:"]
    lines += [f"    #{i} {f}" for i, f in enumerate(read_frames)]
    lines += ["  Previous write of size 8 at 0x10 by thread T2:"]
    lines += [f"    #{i} {f}" for i, f in enumerate(write_frames)]
    if writer_name:
        lines += [f"  Thread T2 '{writer_name}' (tid=2, running) created by main thread at:",
                  "    #0 pthread_create <null> (python3.14+0x1)"]
    lines += ["SUMMARY: ThreadSanitizer: data race"]
    return parse("\n".join(lines) + "\n")[0]


EXT = "(examplelib.cpython-314t-x86_64-linux-gnu.so+0x3)"
PY = "(libpython3.14t.so.1.0+0x1)"


def test_a_mutator_rewriting_buffer_contents_is_a_harness_race():
    """A mutator rewriting an exported bytearray's contents while the
    extension reads it — racy by the buffer protocol's own contract."""
    report = _race(
        [f"digest_update /tmp/c/src/digest.rs:40:9 {EXT}"],
        ["__tsan_memcpy <null> (python3.14+0x9)", f"bytearray_setslice_linear /cpython/Objects/bytearrayobject.c:500:5 {PY}"],
        writer_name="ftm-churn",
    )
    from ftcheck.ci.tsan import HARNESS

    assert attribute(report, EXAMPLE) == HARNESS
    mine, _reached, other = to_findings([report], EXAMPLE, CRATE)
    assert mine == [] and len(other) == 1 and other[0]["message"].startswith("A mutator rewrote")


def test_the_same_rewrite_by_a_non_mutator_thread_is_still_yours():
    report = _race(
        [f"digest_update /tmp/c/src/digest.rs:40:9 {EXT}"],
        [f"bytearray_setslice_linear /cpython/Objects/bytearrayobject.c:500:5 {PY}"],
        writer_name="ftw-pair-3",
    )
    assert attribute(report, EXAMPLE) == YOURS


def test_an_allocation_on_the_other_side_is_a_use_after_free_and_yours():
    """Freed-and-reused memory looks CPython-internal from the frames alone."""
    report = _race(
        [f"PyMutex_LockFast /cpython/Include/cpython/lock.h:60:5 {PY}",
         f"encode_map /tmp/c/src/encode/map.rs:30:9 {EXT}"],
        [f"new_dict /cpython/Objects/dictobject.c:900:5 {PY}"],
    )
    assert attribute(report, EXAMPLE) == YOURS


def test_one_root_cause_from_many_stacks_is_one_finding():
    a = _race([f"next /tmp/c/src/mapping.rs:12:9 {EXT}", f"walk /tmp/c/src/a.rs:1:1 {EXT}"],
              [f"next /tmp/c/src/mapping.rs:12:9 {EXT}"])
    b = _race([f"next /tmp/c/src/mapping.rs:12:9 {EXT}", f"walk /tmp/c/src/b.rs:2:1 {EXT}"],
              [f"next /tmp/c/src/mapping.rs:12:9 {EXT}", f"other /tmp/c/src/c.rs:3:1 {EXT}"])
    mine, _, _ = to_findings([a, b], EXAMPLE, CRATE)
    assert len(mine) == 1 and mine[0]["occurrences"] == 2


def test_a_thin_ffi_wrapper_is_not_the_primary_location():
    report = _race(
        [f"dict_next /tmp/c/src/ffi/dict.rs:20:9 {EXT}", f"encode_model /tmp/c/src/encode/model.rs:61:5 {EXT}"],
        [f"delitem_common /cpython/Objects/dictobject.c:2500:5 {PY}"],
    )
    (finding,), _, _ = to_findings([report], EXAMPLE, CRATE)
    assert finding["primary"]["file"] == "src/encode/model.rs"


MUTATOR_COPY = ["memmove <null> (python3.14+0x9)",
                "_contig_to_contig lowlevel_strided_loops.c (_multiarray_umath.cpython-314t-x86_64-linux-gnu.so+0x7)"]


def test_a_bare_file_name_is_not_a_file_in_the_crate(tmp_path, monkeypatch):
    """An uninstrumented library's debug info can name a source file with no
    directory. Resolved against the working directory — the crate root, in
    the image — it looked like a crate file and became the primary location.
    The test must run from the crate root: that is what exposed it."""
    root = str(tmp_path)
    monkeypatch.chdir(tmp_path)
    # The dependency's side comes first, as it did in the report that exposed it.
    report = _race(MUTATOR_COPY, [f"scan {root}/src/lib.rs:117:14 {EXT}"])
    (finding,), _, _ = to_findings([report], EXAMPLE, root)
    assert finding["primary"]["file"] == "src/lib.rs"
    assert finding["primary"]["line"] == 117
    files = [fr["location"]["file"] for s in finding["stacks"] for fr in s["frames"] if fr["location"]]
    assert "lowlevel_strided_loops.c" in files, "kept as the symbolizer wrote it"


# mutator-numpy-copy.log is trimmed and renamed from a real report: a mutator
# (`ftm-refill_rows`) refills a numpy array in place from Python while two
# methods of the extension read it through the buffer protocol. numpy's copy
# loop has only a bare file name in its debug info.
MUTATOR_LOG = "mutator-numpy-copy.log"


def test_a_mutator_copying_into_a_foreign_buffer_is_a_harness_race():
    """No CPython routine on the mutator's stack: numpy's copy loop calls
    memmove. Still a content rewrite, not the extension's bug."""
    from ftcheck.ci.tsan import HARNESS

    reports = parse(load(MUTATOR_LOG))
    assert len(reports) == 2
    assert [attribute(r, EXAMPLE) for r in reports] == [HARNESS, HARNESS]
    mine, reached, other = to_findings(reports, EXAMPLE, "/src")
    assert mine == [] and reached == []
    assert other and all(f["message"].startswith("A mutator rewrote") for f in other)


def test_a_harness_race_is_located_at_the_extensions_read():
    """The mutator's side is the harness; the line worth reading is yours."""
    _, _, other = to_findings(parse(load(MUTATOR_LOG)), EXAMPLE, "/src")
    assert sorted((f["primary"]["file"], f["primary"]["line"]) for f in other) == [
        ("src/lib.rs", 117),
        ("src/lib.rs", 141),
    ]


def test_a_mutator_that_resizes_while_copying_is_still_yours():
    """A realloc on the mutator's side can free what the extension reads."""
    report = _race(
        [f"scan /tmp/c/src/lib.rs:117:14 {EXT}"],
        [*MUTATOR_COPY, "PyArray_Resize shape.c (_multiarray_umath.cpython-314t-x86_64-linux-gnu.so+0x8)"],
        writer_name="ftm-grow",
    )
    assert attribute(report, EXAMPLE) == YOURS


def test_a_mutator_memmove_inside_a_cpython_container_is_still_yours():
    """list.insert shifts items with memmove under the list's critical
    section; an extension reading the list without one is at fault."""
    report = _race(
        [f"scan /tmp/c/src/lib.rs:117:14 {EXT}"],
        ["__tsan_memmove <null> (python3.14+0x9)", f"list_ass_slice_lock_held /cpython/Objects/listobject.c:700:5 {PY}"],
        writer_name="ftm-shift",
    )
    assert attribute(report, EXAMPLE) == YOURS


def test_a_mutator_copying_through_the_extension_is_still_yours():
    """With a frame of yours on the mutator's side, the copy is your code's."""
    report = _race(
        [f"scan /tmp/c/src/lib.rs:117:14 {EXT}"],
        ["__tsan_memcpy <null> (python3.14+0x9)", f"fill /tmp/c/src/lib.rs:60:9 {EXT}"],
        writer_name="ftm-fill",
    )
    assert attribute(report, EXAMPLE) == YOURS


def test_a_harness_race_run_from_the_crate_root_is_not_located_in_the_dependency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = load(MUTATOR_LOG).replace(" /src/src/", f" {tmp_path}/src/")
    _, _, other = to_findings(parse(text), EXAMPLE, str(tmp_path))
    assert other and {f["primary"]["file"] for f in other} == {"src/lib.rs"}


PYO3_FFI = "/opt/cargo/registry/src/index.crates.io-1949cf8c6b5b557f/pyo3-ffi-0.26.0"

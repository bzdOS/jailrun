#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_gc.py
# PURPOSE: unit tests for runtime/gc.py — pure reconciliation logic, rendering,
#          and the --fix action dispatch
# INTENT: reconcile() is the safety-critical piece (false positives would let
#         --fix tear down a genuinely in-progress run), so it is tested
#         exhaustively against constructed jls/rundb/dataset snapshots with NO
#         subprocess/VM/FreeBSD dependency at all. render()/exit_code_for()
#         are tested the same way test_doctor.py tests doctor.py's render().
#         apply_fixes() is tested with monkeypatched RunDB/_run_ok fakes
#         (never real subprocesses), mirroring test_engine_rundb.py's
#         module-attribute-patching pattern.
# DEPENDENCIES: stdlib (json, os); runtime.gc (Orphan, FixOutcome, reconcile,
#               apply_fixes, render, exit_code_for, run_gc); runtime.rundb
#               (only its module attribute is patched — never a real db)
# PUBLIC_API: each test_* function is callable by pytest; run_all() for direct execution
# END_AI_HEADER

"""
test_gc.py — unit tests for runtime/gc.py.

Run with pytest:
    python3 -m pytest runtime/test_gc.py -v
"""

import json
import os

from runtime.gc import (
    Orphan,
    FixOutcome,
    reconcile,
    apply_fixes,
    render,
    exit_code_for,
    run_gc,
)


# ---------------------------------------------------------------------------
# reconcile() — category 1: stale rundb rows
# ---------------------------------------------------------------------------

# CONTRACT: a rundb row with status='running' whose jail_name is NOT in
# known_jails is flagged as a stale_rundb_row orphan.
def test_stale_running_row_with_no_matching_jail():
    """A 'running' rundb row with no matching live jail is a stale_rundb_row orphan."""
    known_jails = []  # jail is gone
    rundb_rows = [
        {"jail_name": "jailrun-gone", "dataset": "jailrun/runs/gone", "status": "running"},
    ]
    orphans = reconcile(known_jails, rundb_rows, [])
    assert len(orphans) == 1
    o = orphans[0]
    assert o.kind == "stale_rundb_row"
    assert o.identifier == "jailrun-gone"
    assert "jailrun/runs/gone" in o.detail
    print("PASS test_stale_running_row_with_no_matching_jail")


# ---------------------------------------------------------------------------
# reconcile() — category 2: orphaned live jails
# ---------------------------------------------------------------------------

# CONTRACT: a live jailrun-* jail with no rundb row at all is an orphaned_jail.
def test_orphaned_jail_with_no_rundb_row():
    """A live jailrun-* jail with no rundb row at all is flagged orphaned_jail."""
    known_jails = ["jailrun-noRow"]
    rundb_rows = []
    orphans = reconcile(known_jails, rundb_rows, [])
    assert len(orphans) == 1
    o = orphans[0]
    assert o.kind == "orphaned_jail"
    assert o.identifier == "jailrun-noRow"
    assert "no rundb row" in o.detail
    print("PASS test_orphaned_jail_with_no_rundb_row")


# CONTRACT: a live jailrun-* jail whose rundb row already says 'exited' is
# flagged orphaned_jail (rundb thinks it's gone, but it's still live).
def test_orphaned_jail_with_exited_rundb_row():
    """A live jail whose rundb row says 'exited' is flagged orphaned_jail."""
    known_jails = ["jailrun-stillup"]
    rundb_rows = [
        {"jail_name": "jailrun-stillup", "dataset": "jailrun/runs/stillup", "status": "exited"},
    ]
    orphans = reconcile(known_jails, rundb_rows, [])
    assert len(orphans) == 1
    o = orphans[0]
    assert o.kind == "orphaned_jail"
    assert o.identifier == "jailrun-stillup"
    assert "exited" in o.detail
    print("PASS test_orphaned_jail_with_exited_rundb_row")


# CONTRACT: a live jailrun-* jail whose rundb row says 'killed' is likewise
# flagged orphaned_jail.
def test_orphaned_jail_with_killed_rundb_row():
    """A live jail whose rundb row says 'killed' is flagged orphaned_jail."""
    known_jails = ["jailrun-stillup2"]
    rundb_rows = [
        {"jail_name": "jailrun-stillup2", "dataset": "jailrun/runs/stillup2", "status": "killed"},
    ]
    orphans = reconcile(known_jails, rundb_rows, [])
    assert len(orphans) == 1
    assert orphans[0].kind == "orphaned_jail"
    print("PASS test_orphaned_jail_with_killed_rundb_row")


# CONTRACT: a live jail whose name does NOT match the jailrun-<handle>
# convention is never flagged, even with no rundb row — it isn't ours.
def test_non_jailrun_jail_is_ignored():
    """A live jail not matching the jailrun- naming convention is never flagged."""
    known_jails = ["some-other-jail"]
    rundb_rows = []
    orphans = reconcile(known_jails, rundb_rows, [])
    assert orphans == []
    print("PASS test_non_jailrun_jail_is_ignored")


# ---------------------------------------------------------------------------
# reconcile() — category 3: orphaned datasets
# ---------------------------------------------------------------------------

# CONTRACT: a runs/<id> dataset with no live jail and no running rundb row is
# an orphaned_dataset.
def test_orphaned_dataset_with_no_running_jail_or_row():
    """A dataset with no live jail and no running rundb row is orphaned_dataset."""
    known_jails = []
    rundb_rows = []
    known_datasets = [{"run_id": "abc123", "dataset": "jailrun/runs/abc123"}]
    orphans = reconcile(known_jails, rundb_rows, known_datasets)
    assert len(orphans) == 1
    o = orphans[0]
    assert o.kind == "orphaned_dataset"
    assert o.identifier == "jailrun/runs/abc123"
    assert "abc123" in o.detail
    print("PASS test_orphaned_dataset_with_no_running_jail_or_row")


# CONTRACT: a dataset whose run's rundb row already says 'exited'/'killed' —
# not just "no row at all" — is also orphaned_dataset.
def test_orphaned_dataset_with_exited_rundb_row():
    """A dataset for an already-exited run (rundb row present, not 'running') is orphaned_dataset."""
    known_jails = []
    rundb_rows = [
        {"jail_name": "jailrun-done", "dataset": "jailrun/runs/done", "status": "exited"},
    ]
    known_datasets = [{"run_id": "done", "dataset": "jailrun/runs/done"}]
    orphans = reconcile(known_jails, rundb_rows, known_datasets)
    dataset_orphans = [o for o in orphans if o.kind == "orphaned_dataset"]
    assert len(dataset_orphans) == 1
    assert dataset_orphans[0].identifier == "jailrun/runs/done"
    print("PASS test_orphaned_dataset_with_exited_rundb_row")


# CONTRACT: a dataset whose rundb row STILL says 'running' (even though its
# jail is gone from jls — already flagged separately as a stale_rundb_row) is
# NOT independently flagged as an orphaned_dataset — gc must never tear down
# storage rundb still claims is live, in the same pass that first notices the
# inconsistency.
def test_dataset_not_flagged_when_rundb_row_still_running():
    """A dataset is not flagged orphaned_dataset while its rundb row still says 'running'."""
    known_jails = []  # jail already gone
    rundb_rows = [
        {"jail_name": "jailrun-stale", "dataset": "jailrun/runs/stale", "status": "running"},
    ]
    known_datasets = [{"run_id": "stale", "dataset": "jailrun/runs/stale"}]
    orphans = reconcile(known_jails, rundb_rows, known_datasets)
    kinds = {o.kind for o in orphans}
    assert "orphaned_dataset" not in kinds
    # It IS still flagged as a stale_rundb_row (category 1) — the two
    # categories are not the same check.
    assert "stale_rundb_row" in kinds
    print("PASS test_dataset_not_flagged_when_rundb_row_still_running")


# ---------------------------------------------------------------------------
# reconcile() — the false-positive-safety property: a genuinely still-running,
# fully consistent run must NEVER be flagged in any category.
# ---------------------------------------------------------------------------

# CONTRACT: a real jail + a 'running' rundb row + its own dataset, all
# mutually consistent, produces ZERO orphans across all three categories.
def test_genuinely_running_consistent_run_never_flagged():
    """A live jail + running rundb row + its own dataset is never flagged as any orphan kind."""
    known_jails = ["jailrun-live1"]
    rundb_rows = [
        {"jail_name": "jailrun-live1", "dataset": "jailrun/runs/live1", "status": "running"},
    ]
    known_datasets = [{"run_id": "live1", "dataset": "jailrun/runs/live1"}]
    orphans = reconcile(known_jails, rundb_rows, known_datasets)
    assert orphans == [], f"a genuinely in-progress run must never be flagged, got: {orphans}"
    print("PASS test_genuinely_running_consistent_run_never_flagged")


# CONTRACT: the clean case — multiple consistent runs (some running, some
# already exited with no leftover jail/dataset) — produces zero orphans.
def test_clean_case_nothing_orphaned():
    """A fully consistent multi-run snapshot (running + cleanly-exited) has no orphans."""
    known_jails = ["jailrun-live2"]
    rundb_rows = [
        {"jail_name": "jailrun-live2", "dataset": "jailrun/runs/live2", "status": "running"},
        {"jail_name": "jailrun-done2", "dataset": "jailrun/runs/done2", "status": "exited"},
    ]
    # done2's dataset was already destroyed by --rm; only live2's remains.
    known_datasets = [{"run_id": "live2", "dataset": "jailrun/runs/live2"}]
    orphans = reconcile(known_jails, rundb_rows, known_datasets)
    assert orphans == []
    print("PASS test_clean_case_nothing_orphaned")


# ---------------------------------------------------------------------------
# reconcile() — availability handling: None means "couldn't check", never
# treated the same as an empty list.
# ---------------------------------------------------------------------------

# CONTRACT: known_jails=None (jls unavailable) skips categories 1 and 2
# entirely — a 'running' rundb row is NOT guessed to be stale.
def test_none_known_jails_skips_jail_categories():
    """known_jails=None must not cause a 'running' rundb row to be guessed stale."""
    rundb_rows = [
        {"jail_name": "jailrun-x", "dataset": "jailrun/runs/x", "status": "running"},
    ]
    orphans = reconcile(None, rundb_rows, [])
    assert orphans == [], "unavailable jls data must never be treated as 'zero jails'"
    print("PASS test_none_known_jails_skips_jail_categories")


# CONTRACT: rundb_rows=None (rundb unreadable) skips categories 1 and 2
# entirely — a live jailrun-* jail is NOT guessed to be orphaned.
def test_none_rundb_rows_skips_jail_categories():
    """rundb_rows=None must not cause a live jail to be guessed orphaned."""
    known_jails = ["jailrun-y"]
    orphans = reconcile(known_jails, None, [])
    assert orphans == [], "unavailable rundb data must never be treated as 'zero rows'"
    print("PASS test_none_rundb_rows_skips_jail_categories")


# CONTRACT: known_datasets=None (store backend unavailable) skips only
# category 3 — categories 1/2 still run normally on the available data.
def test_none_known_datasets_skips_only_dataset_category():
    """known_datasets=None skips category 3 but not categories 1/2."""
    known_jails = []
    rundb_rows = [
        {"jail_name": "jailrun-z", "dataset": "jailrun/runs/z", "status": "running"},
    ]
    orphans = reconcile(known_jails, rundb_rows, None)
    assert len(orphans) == 1
    assert orphans[0].kind == "stale_rundb_row"
    print("PASS test_none_known_datasets_skips_only_dataset_category")


# ---------------------------------------------------------------------------
# render() — text
# ---------------------------------------------------------------------------

# CONTRACT: render() with no orphans and no notes prints "no orphans found".
def test_render_text_clean():
    """Text render with no orphans reports 'no orphans found'."""
    output = render([], None, [], fmt="text")
    assert "no orphans found" in output
    print("PASS test_render_text_clean")


# CONTRACT: render() surfaces notes (degraded-capability diagnostics) as
# [NOTE] lines even when there are no orphans.
def test_render_text_with_notes_only():
    """Notes (e.g. 'jls unavailable') render as [NOTE] lines."""
    output = render([], None, ["could not enumerate jails via `jls -n name`"], fmt="text")
    assert "[NOTE]" in output
    assert "jls -n name" in output
    assert "no orphans found" in output
    print("PASS test_render_text_with_notes_only")


# CONTRACT: render() in dry-run mode (fixes=None) shows each orphan + its Fix
# line, with no [FIXED]/[FIX-FAILED] markers.
def test_render_text_dry_run_orphans():
    """Dry-run text render shows [ORPHAN]/Fix lines, no fix outcome markers."""
    orphans = [
        Orphan("stale_rundb_row", "jailrun-a", "detail-a", "record_exit(...)"),
        Orphan("orphaned_jail", "jailrun-b", "detail-b", "jail -r jailrun-b"),
    ]
    output = render(orphans, None, [], fmt="text")
    assert "[ORPHAN] stale_rundb_row: jailrun-a" in output
    assert "detail-a" in output
    assert "Fix: record_exit(...)" in output
    assert "[ORPHAN] orphaned_jail: jailrun-b" in output
    assert "[FIXED]" not in output
    assert "[FIX-FAILED]" not in output
    print("PASS test_render_text_dry_run_orphans")


# CONTRACT: render() in --fix mode shows [FIXED] for successful fixes and
# [FIX-FAILED] for failed ones, matched to the right orphan.
def test_render_text_fix_mode_outcomes():
    """--fix-mode text render shows [FIXED]/[FIX-FAILED] matched per orphan."""
    orphans = [
        Orphan("orphaned_jail", "jailrun-ok", "d1", "jail -r jailrun-ok"),
        Orphan("orphaned_dataset", "jailrun/runs/bad", "d2", "destroy dataset/clone jailrun/runs/bad"),
    ]
    fixes = [
        FixOutcome("orphaned_jail", "jailrun-ok", True, "jail -r succeeded"),
        FixOutcome("orphaned_dataset", "jailrun/runs/bad", False, "zfs destroy failed (rc=1): busy"),
    ]
    output = render(orphans, fixes, [], fmt="text")
    lines = output.splitlines()
    # 3 lines per orphan: [ORPHAN]/Fix/outcome marker — verify they line up
    # with the RIGHT orphan, not just present somewhere in the output.
    assert lines[0] == "[ORPHAN] orphaned_jail: jailrun-ok — d1"
    assert lines[1].strip() == "Fix: jail -r jailrun-ok"
    assert lines[2].strip() == "[FIXED] jail -r succeeded"
    assert lines[3] == "[ORPHAN] orphaned_dataset: jailrun/runs/bad — d2"
    assert lines[4].strip() == "Fix: destroy dataset/clone jailrun/runs/bad"
    assert lines[5].strip() == "[FIX-FAILED] zfs destroy failed (rc=1): busy"
    print("PASS test_render_text_fix_mode_outcomes")


# ---------------------------------------------------------------------------
# render() — json
# ---------------------------------------------------------------------------

# CONTRACT: JSON render is valid JSON with orphans/fixes/notes keys; fixes is
# null in dry-run mode.
def test_render_json_dry_run_structure():
    """JSON render (dry run) has orphans/notes populated and fixes=null."""
    orphans = [Orphan("stale_rundb_row", "jailrun-a", "detail-a", "record_exit(...)")]
    output = render(orphans, None, ["a note"], fmt="json")
    data = json.loads(output)
    assert data["fixes"] is None
    assert data["notes"] == ["a note"]
    assert len(data["orphans"]) == 1
    o = data["orphans"][0]
    assert o["kind"] == "stale_rundb_row"
    assert o["identifier"] == "jailrun-a"
    assert o["detail"] == "detail-a"
    assert o["fix_action"] == "record_exit(...)"
    print("PASS test_render_json_dry_run_structure")


# CONTRACT: JSON render (--fix mode) has a populated fixes list, one entry per
# orphan, with kind/identifier/ok/detail keys.
def test_render_json_fix_mode_structure():
    """JSON render (--fix mode) has a fixes list matching orphans 1:1."""
    orphans = [Orphan("orphaned_jail", "jailrun-b", "d", "jail -r jailrun-b")]
    fixes = [FixOutcome("orphaned_jail", "jailrun-b", True, "jail -r succeeded")]
    output = render(orphans, fixes, [], fmt="json")
    data = json.loads(output)
    assert isinstance(data["fixes"], list)
    assert len(data["fixes"]) == 1
    f = data["fixes"][0]
    assert f["kind"] == "orphaned_jail"
    assert f["ok"] is True
    assert f["detail"] == "jail -r succeeded"
    print("PASS test_render_json_fix_mode_structure")


# ---------------------------------------------------------------------------
# exit_code_for()
# ---------------------------------------------------------------------------

# CONTRACT: dry run (fixes=None), no orphans -> exit code 0.
def test_exit_code_dry_run_clean():
    assert exit_code_for([], None) == 0
    print("PASS test_exit_code_dry_run_clean")


# CONTRACT: dry run (fixes=None), any orphans -> exit code 1.
def test_exit_code_dry_run_with_orphans():
    orphans = [Orphan("orphaned_jail", "jailrun-a", "d", "jail -r jailrun-a")]
    assert exit_code_for(orphans, None) == 1
    print("PASS test_exit_code_dry_run_with_orphans")


# CONTRACT: --fix mode, all fixes succeeded (or nothing to fix) -> exit code 0.
def test_exit_code_fix_mode_all_ok():
    orphans = [Orphan("orphaned_jail", "jailrun-a", "d", "jail -r jailrun-a")]
    fixes = [FixOutcome("orphaned_jail", "jailrun-a", True, "ok")]
    assert exit_code_for(orphans, fixes) == 0
    assert exit_code_for([], []) == 0
    print("PASS test_exit_code_fix_mode_all_ok")


# CONTRACT: --fix mode, any fix failed -> exit code 1.
def test_exit_code_fix_mode_some_failed():
    orphans = [
        Orphan("orphaned_jail", "jailrun-a", "d", "jail -r jailrun-a"),
        Orphan("orphaned_dataset", "jailrun/runs/b", "d", "destroy dataset/clone jailrun/runs/b"),
    ]
    fixes = [
        FixOutcome("orphaned_jail", "jailrun-a", True, "ok"),
        FixOutcome("orphaned_dataset", "jailrun/runs/b", False, "busy"),
    ]
    assert exit_code_for(orphans, fixes) == 1
    print("PASS test_exit_code_fix_mode_some_failed")


# ---------------------------------------------------------------------------
# apply_fixes() — dispatch + defensive continuation, using fakes (no real
# subprocess/db). Monkeypatches runtime.rundb.RunDB (module attribute, same
# pattern test_engine_rundb.py uses) and runtime.gc._run_ok.
# ---------------------------------------------------------------------------

# CONTRACT: apply_fixes() calls the right cleanup action per orphan kind
# (record_exit for stale_rundb_row, jail -r for orphaned_jail, destroy for
# orphaned_dataset) and CONTINUES past a failure in one item (the dataset
# destroy fails, including its -f retry) rather than aborting the loop.
def test_apply_fixes_dispatches_and_continues_past_failure():
    """apply_fixes() dispatches correctly per kind and continues past one failure."""
    import runtime.gc as gc_module
    import runtime.rundb as rundb_module

    calls: list = []

    class _FakeRunDB:
        def __init__(self, path=None) -> None:  # noqa: ARG002
            pass

        def record_exit(self, jail_name, status, exit_code) -> None:
            calls.append(("record_exit", jail_name, status, exit_code))

    def _fake_run_ok(argv, timeout=30.0):  # noqa: ARG001
        calls.append(("run_ok", tuple(argv)))
        if argv[:2] == ["jail", "-r"]:
            return 0, "", ""
        if argv[:2] == ["zfs", "destroy"]:
            # Fails on both the plain attempt and the -f retry, on purpose —
            # proves apply_fixes() still returns an outcome for it (rather
            # than raising) and keeps going.
            return 1, "", "simulated: dataset is busy"
        return -1, "", "unexpected argv in test fake"

    orig_rundb_class = rundb_module.RunDB
    orig_run_ok = gc_module._run_ok
    saved_backend = os.environ.get("JAILRUN_STORE_BACKEND")
    os.environ["JAILRUN_STORE_BACKEND"] = "zfs"
    rundb_module.RunDB = _FakeRunDB
    gc_module._run_ok = _fake_run_ok
    try:
        orphans = [
            Orphan("stale_rundb_row", "jailrun-a", "d-a", "record_exit(...)"),
            Orphan("orphaned_dataset", "jailrun/runs/b", "d-b", "destroy dataset/clone jailrun/runs/b"),
            Orphan("orphaned_jail", "jailrun-c", "d-c", "jail -r jailrun-c"),
        ]
        outcomes = apply_fixes(orphans)
    finally:
        rundb_module.RunDB = orig_rundb_class
        gc_module._run_ok = orig_run_ok
        if saved_backend is None:
            os.environ.pop("JAILRUN_STORE_BACKEND", None)
        else:
            os.environ["JAILRUN_STORE_BACKEND"] = saved_backend

    assert len(outcomes) == 3, "one outcome per orphan, even though the middle one failed"

    assert outcomes[0].kind == "stale_rundb_row"
    assert outcomes[0].identifier == "jailrun-a"
    assert outcomes[0].ok is True
    assert ("record_exit", "jailrun-a", "killed", None) in calls

    assert outcomes[1].kind == "orphaned_dataset"
    assert outcomes[1].ok is False  # both destroy attempts failed
    assert "busy" in outcomes[1].detail

    # Processing continued past the failure: the third orphan was still handled.
    assert outcomes[2].kind == "orphaned_jail"
    assert outcomes[2].identifier == "jailrun-c"
    assert outcomes[2].ok is True
    assert ("run_ok", ("jail", "-r", "jailrun-c")) in calls
    print("PASS test_apply_fixes_dispatches_and_continues_past_failure")


# CONTRACT: an UNEXPECTED exception raised by one cleanup action (not just a
# nonzero return code) is caught per-item and does not stop later items from
# being processed — the same defense-in-depth property engine.py relies on.
def test_apply_fixes_continues_after_unexpected_exception():
    """A cleanup action raising an unexpected exception doesn't abort the rest."""
    import runtime.rundb as rundb_module

    class _BrokenRunDB:
        def __init__(self, path=None) -> None:  # noqa: ARG002
            pass

        def record_exit(self, jail_name, status, exit_code) -> None:  # noqa: ARG002
            raise RuntimeError("simulated rundb corruption")

    import runtime.gc as gc_module

    def _fake_run_ok(argv, timeout=30.0):  # noqa: ARG001
        if argv[:2] == ["jail", "-r"]:
            return 0, "", ""
        return -1, "", "unexpected"

    orig_rundb_class = rundb_module.RunDB
    orig_run_ok = gc_module._run_ok
    rundb_module.RunDB = _BrokenRunDB
    gc_module._run_ok = _fake_run_ok
    try:
        orphans = [
            Orphan("stale_rundb_row", "jailrun-broken", "d", "record_exit(...)"),
            Orphan("orphaned_jail", "jailrun-fine", "d", "jail -r jailrun-fine"),
        ]
        outcomes = apply_fixes(orphans)
    finally:
        rundb_module.RunDB = orig_rundb_class
        gc_module._run_ok = orig_run_ok

    assert len(outcomes) == 2
    assert outcomes[0].ok is False
    assert "simulated rundb corruption" in outcomes[0].detail
    # The second item was still processed despite the first raising.
    assert outcomes[1].kind == "orphaned_jail"
    assert outcomes[1].ok is True
    print("PASS test_apply_fixes_continues_after_unexpected_exception")


# ---------------------------------------------------------------------------
# run_gc() — light integration check: must never raise on this host, whether
# or not jls/zfs/a real rundb happen to be present (Linux dev host or a real
# FreeBSD box) — this is the property runtime/gc.py's module docstring/
# invariants promise for `jailrun gc` itself.
# ---------------------------------------------------------------------------

# CONTRACT: run_gc(fix=False) completes without raising on this host and
# returns the documented shape, regardless of whether jls/zfs are present.
def test_run_gc_dry_run_does_not_raise_on_this_host():
    """run_gc(fix=False) never raises, on any host (jls/zfs present or not)."""
    orphans, fixes, notes = run_gc(fix=False)
    assert isinstance(orphans, list)
    assert fixes is None
    assert isinstance(notes, list)
    # Whatever it found, render()/exit_code_for() must handle it without raising.
    render(orphans, fixes, notes, fmt="text")
    render(orphans, fixes, notes, fmt="json")
    exit_code_for(orphans, fixes)
    print("PASS test_run_gc_dry_run_does_not_raise_on_this_host")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_stale_running_row_with_no_matching_jail,
    test_orphaned_jail_with_no_rundb_row,
    test_orphaned_jail_with_exited_rundb_row,
    test_orphaned_jail_with_killed_rundb_row,
    test_non_jailrun_jail_is_ignored,
    test_orphaned_dataset_with_no_running_jail_or_row,
    test_orphaned_dataset_with_exited_rundb_row,
    test_dataset_not_flagged_when_rundb_row_still_running,
    test_genuinely_running_consistent_run_never_flagged,
    test_clean_case_nothing_orphaned,
    test_none_known_jails_skips_jail_categories,
    test_none_rundb_rows_skips_jail_categories,
    test_none_known_datasets_skips_only_dataset_category,
    test_render_text_clean,
    test_render_text_with_notes_only,
    test_render_text_dry_run_orphans,
    test_render_text_fix_mode_outcomes,
    test_render_json_dry_run_structure,
    test_render_json_fix_mode_structure,
    test_exit_code_dry_run_clean,
    test_exit_code_dry_run_with_orphans,
    test_exit_code_fix_mode_all_ok,
    test_exit_code_fix_mode_some_failed,
    test_apply_fixes_dispatches_and_continues_past_failure,
    test_apply_fixes_continues_after_unexpected_exception,
    test_run_gc_dry_run_does_not_raise_on_this_host,
]


# run_all:start
#   purpose: execute every function in TESTS, collect failures, report pass/fail counts
#   input: none
#   output: none (results printed to stdout)
#   sideEffects: prints PASS/FAIL/ERROR lines per test; calls sys.exit(1) if any failure
def run_all():
    import sys  # noqa: PLC0415
    failures = []
    for fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            print(f"FAIL {fn.__name__}: {exc}")
            failures.append(fn.__name__)
        except Exception as exc:
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            failures.append(fn.__name__)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"All {len(TESTS)} tests passed.")
# run_all:end


if __name__ == "__main__":
    run_all()

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/gc.py
# PURPOSE: find and clean up orphans left behind when jailrun's OWN process is
#          killed mid-run (not the jailed workload crashing — engine.py's
#          finally/timeout logic already handles that case)
# INTENT: `jailrun gc [--fix] [--format text|json]` — a health-check-shaped
#         reconciliation between three independent sources of truth (`jls -n`,
#         runtime/rundb.py's ledger, and the store's runs dataset/directory
#         tree) that can disagree after a crash: a live jail with no rundb
#         row, a rundb row stuck at status='running' after its jail is gone,
#         or a ZFS clone/plaindir copy that outlived both.
# DEPENDENCIES: stdlib only (subprocess, os, re, shutil, logging, json,
#               dataclasses); runtime.rundb.RunDB (imported lazily, same
#               pattern as engine.py/cli.py — never at module import time)
# PUBLIC_API: Orphan, FixOutcome (dataclasses); reconcile() (pure logic);
#             collect(), apply_fixes() (glue — real subprocess/RunDB I/O);
#             run_gc(), render(), exit_code_for()
# END_AI_HEADER

# START_INVARIANTS
# - reconcile() is a PURE function: plain lists/dicts in, a list[Orphan] out,
#   no subprocess/filesystem/db access. It is the only place orphan-detection
#   DECISIONS are made, so it can be tested exhaustively without a VM.
# - reconcile() must NEVER flag a run as an orphan when the three sources of
#   truth AGREE it is genuinely still in progress (live jail + 'running'
#   rundb row + its own dataset). False positives here are dangerous: --fix
#   would tear down a real in-flight run.
# - Any input list reconcile() cannot determine on this host (jls missing,
#   rundb unreadable, dataset backend unavailable) MUST be passed as None,
#   never as an empty list — collect() enforces this distinction. An empty
#   list means "genuinely checked, found nothing"; None means "could not
#   check", and reconcile() skips the affected categories entirely rather
#   than guessing (guessing empty would flag every real running row/jail as
#   an orphan the moment jls/rundb/zfs happen to be unavailable).
# - apply_fixes() wraps each individual cleanup action in its own try/except:
#   one failing item (a jail already gone, a dataset already destroyed by a
#   racing gc run) must never stop the rest from being attempted.
# END_INVARIANTS

"""
runtime/gc.py — orphan reconciliation for jailrun.

Three independent sources of truth can disagree after jailrun's own process
(not the jailed workload) is killed mid-run, between `jail -c` succeeding and
`jail -r` running in engine.py's finally block:

  1. jls -n           — jails actually alive on this host right now
  2. runtime/rundb.py  — jailrun's own ledger of what it thinks is running
  3. store's runs tree — ZFS clones / plaindir copies backing each run

`jailrun gc` reconciles the three into three orphan categories:

  stale_rundb_row   — rundb says 'running' but the jail is gone from `jls -n`
  orphaned_jail      — a live jailrun-* jail with no rundb row, or one whose
                        row already says 'exited'/'killed'
  orphaned_dataset   — a runs/<id> dataset/directory with no live jail AND no
                        'running' rundb row for it

Default (no --fix): dry-run report, exit 0 if clean / 1 if anything found —
usable as a health check in scripts. --fix: actually clean up (record_exit,
jail -r, destroy dataset), reporting success/failure per item.

Run with pytest (see runtime/test_gc.py):
    python3 -m pytest runtime/test_gc.py -v
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger("jailrun.gc")

JAILRUN_PREFIX = "jailrun-"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Orphan:start
#   purpose: represent one detected crash-artifact orphan
#   input:
#     kind: str — 'stale_rundb_row' | 'orphaned_jail' | 'orphaned_dataset'
#     identifier: str — jail_name (stale_rundb_row/orphaned_jail) or
#                 dataset name / plaindir path (orphaned_dataset)
#     detail: str — human-readable explanation of why this was flagged
#     fix_action: str — description of what --fix does for this kind
#   output: an Orphan instance
#   sideEffects: none
@dataclass(frozen=True)
class Orphan:
    """One detected orphan: a disagreement between jls/rundb/store state."""
    kind: str
    identifier: str
    detail: str
    fix_action: str
# Orphan:end


# FixOutcome:start
#   purpose: represent the result of attempting to clean up one Orphan
#   input:
#     kind: str — same as the Orphan's kind
#     identifier: str — same as the Orphan's identifier
#     ok: bool — True iff the cleanup action succeeded
#     detail: str — what happened (success message or error text)
#   output: a FixOutcome instance
#   sideEffects: none
@dataclass(frozen=True)
class FixOutcome:
    """Result of one --fix cleanup action."""
    kind: str
    identifier: str
    ok: bool
    detail: str
# FixOutcome:end


# ---------------------------------------------------------------------------
# Pure reconciliation logic — no I/O, fully unit-testable
# ---------------------------------------------------------------------------

# reconcile:start
#   purpose: compute the three orphan categories from plain snapshots of
#            jls/rundb/dataset state — the ONLY place orphan-detection
#            decisions are made
#   input:
#     known_jails: list[str] | None — jail names from `jls -n name` on this
#                  host right now, or None if that could not be determined
#                  (jls missing/failed — NOT the same as "zero jails")
#     rundb_rows: list[dict] | None — rows as returned by
#                 RunDB.list_runs(status=None) (keys: jail_name, dataset,
#                 status, ...), or None if the rundb could not be read
#     known_datasets: list[dict] | None — entries {"run_id": str,
#                     "dataset": str} for every entry under the store's runs
#                     tree, where "dataset" matches the exact string RunDB
#                     stores in its own 'dataset' column (full ZFS dataset
#                     name or plaindir absolute path); None if the store's
#                     backend could not be enumerated on this host
#   output:
#     orphans: list[Orphan] — every detected orphan across the categories
#              whose required inputs were available; categories whose inputs
#              are None contribute nothing (never guessed)
#   sideEffects: none (pure function)
#   rationale: categories 1+2 both need known_jails AND rundb_rows; category 3
#              additionally needs known_datasets. A jailrun-<id> jail's own
#              "is this legitimately still running" question is always
#              answered by asking whether ITS rundb row (looked up by the
#              exact jail_name, not by dataset string matching) says
#              status=='running' — this is what keeps a real in-flight run
#              (live jail + running row + live dataset) from ever being
#              flagged, in any of the three categories.
def reconcile(
    known_jails: list[str] | None,
    rundb_rows: list[dict] | None,
    known_datasets: list[dict] | None,
) -> list[Orphan]:
    """Pure reconciliation: compute orphans from plain jls/rundb/dataset snapshots."""
    orphans: list[Orphan] = []

    if known_jails is None or rundb_rows is None:
        # Can't safely compute categories 1/2 without both sources — guessing
        # "zero jails" or "zero rows" here would flag real running work.
        return orphans

    jails_set = set(known_jails)
    # jail_name is rundb's PRIMARY KEY, so this is 1:1; last-wins is harmless
    # even if a caller ever passed duplicate rows.
    rundb_by_name = {row["jail_name"]: row for row in rundb_rows}

    # START_STALE_RUNDB_ROWS
    # Category 1: rundb says 'running' but the jail is gone from `jls -n`.
    for row in rundb_rows:
        if row.get("status") == "running" and row.get("jail_name") not in jails_set:
            orphans.append(Orphan(
                kind="stale_rundb_row",
                identifier=row["jail_name"],
                detail=(
                    f"rundb row status='running' but jail not present in "
                    f"`jls -n` (dataset={row.get('dataset')!r})"
                ),
                fix_action="record_exit(jail_name, status='killed', exit_code=None)",
            ))
    # END_STALE_RUNDB_ROWS

    # START_ORPHANED_LIVE_JAILS
    # Category 2: a live jailrun-* jail with no rundb row, or one whose row
    # already says the run should be over.
    for name in known_jails:
        if not name.startswith(JAILRUN_PREFIX):
            continue  # not ours — some unrelated jail on this host
        row = rundb_by_name.get(name)
        if row is None:
            orphans.append(Orphan(
                kind="orphaned_jail",
                identifier=name,
                detail="jail is live in `jls -n` but has no rundb row at all",
                fix_action=f"jail -r {name}",
            ))
        elif row.get("status") in ("exited", "killed"):
            orphans.append(Orphan(
                kind="orphaned_jail",
                identifier=name,
                detail=f"jail is live in `jls -n` but rundb row says status={row.get('status')!r}",
                fix_action=f"jail -r {name}",
            ))
        # else: status == 'running' and the jail is alive — a genuinely
        # in-progress run. Never flagged.
    # END_ORPHANED_LIVE_JAILS

    # START_ORPHANED_DATASETS
    # Category 3: a runs/<id> dataset/directory with no live jail AND no
    # 'running' rundb row for the run it belongs to.
    if known_datasets is not None:
        for entry in known_datasets:
            run_id = entry["run_id"]
            dataset = entry["dataset"]
            expected_jail = f"{JAILRUN_PREFIX}{run_id}"
            if expected_jail in jails_set:
                continue  # its jail is alive — not orphaned
            row = rundb_by_name.get(expected_jail)
            if row is not None and row.get("status") == "running":
                # rundb still thinks this run is in progress (already flagged,
                # if inconsistent with jls, as a stale_rundb_row above) — never
                # independently tear down a dataset rundb still claims is live.
                continue
            orphans.append(Orphan(
                kind="orphaned_dataset",
                identifier=dataset,
                detail=(
                    f"dataset/run dir for run_id={run_id!r} has no live jail "
                    "and no running rundb row"
                ),
                fix_action=f"destroy dataset/clone {dataset}",
            ))
    # END_ORPHANED_DATASETS

    return orphans
# reconcile:end


# ---------------------------------------------------------------------------
# Small local subprocess helper — mirrors store.py's _run_ok pattern (list
# argv, never shell=True, tolerates failure) without importing store.py's
# internals.
# ---------------------------------------------------------------------------

# _run_ok:start
#   purpose: run an external command without raising; tolerate a missing
#            binary or timeout the same way store.py's own _run_ok does
#   input:
#     argv: list[str] — command and arguments (never shell=True)
#     timeout: float — seconds before killing the process
#   output:
#     tuple[int, str, str] — (returncode, stdout, stderr); rc=-1 with an
#             error message in stderr on timeout or spawn failure
#   sideEffects: spawns subprocess via subprocess.run(capture_output=True)
def _run_ok(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """Run *argv*; return (rc, stdout, stderr) without raising."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("gc: command failed/unavailable (tolerated): %s: %s", argv, exc)
        return -1, "", str(exc)
    return result.returncode, result.stdout, result.stderr
# _run_ok:end


# ---------------------------------------------------------------------------
# Store backend config — small local mirror of store.py's env-var helpers
# (JAILRUN_STORE_BACKEND / JAILRUN_ZPOOL / JAILRUN_MOUNTPOINT_BASE). Kept
# local rather than importing store.py's private helpers, per the module's
# design constraint: gc.py shells out to jls/zfs itself instead of driving
# them through Store's own subprocess-running methods.
# ---------------------------------------------------------------------------

def _get_backend() -> str:
    """Return 'zfs' or 'plaindir' from JAILRUN_STORE_BACKEND; default 'zfs'."""
    val = os.environ.get("JAILRUN_STORE_BACKEND", "zfs").strip().lower()
    return val if val in ("zfs", "plaindir") else "zfs"


def _get_zpool() -> str:
    """Return ZFS pool name from JAILRUN_ZPOOL; default 'jailrun'."""
    return os.environ.get("JAILRUN_ZPOOL", "jailrun").strip()


def _get_mountpoint_base() -> str:
    """Return the plaindir tree root from JAILRUN_MOUNTPOINT_BASE; default '/var/jailrun'."""
    return os.environ.get("JAILRUN_MOUNTPOINT_BASE", "/var/jailrun").strip()


# ---------------------------------------------------------------------------
# Glue: real jls / rundb / dataset enumeration (all best-effort, never raise)
# ---------------------------------------------------------------------------

# _list_jail_names:start
#   purpose: enumerate jail names currently visible on this host via jls(8)
#   input: none
#   output:
#     names: list[str] | None — jail names (possibly empty list if genuinely
#            zero jails are running), or None if jls itself is unavailable
#            or failed (non-FreeBSD host, jail subsystem absent, etc.)
#   sideEffects: runs 'jls -n name' with a bounded timeout; never raises
def _list_jail_names() -> list[str] | None:
    """Return jail names from `jls -n name`, or None if jls is unavailable."""
    try:
        proc = subprocess.run(
            ["jls", "-n", "name"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("gc: jls unavailable: %s", exc)
        return None
    if proc.returncode != 0:
        log.debug("gc: jls exited %d: %s", proc.returncode, proc.stderr.strip())
        return None
    names: list[str] = []
    for line in proc.stdout.splitlines():
        m = re.search(r"\bname=(\S+)", line)
        if m:
            names.append(m.group(1))
    return names
# _list_jail_names:end


# _list_zfs_run_datasets:start
#   purpose: enumerate ZFS datasets under the store's runs tree
#   input:
#     runs_ds: str — full parent dataset name, e.g. 'jailrun/runs'
#   output:
#     entries: list[dict] | None — [{"run_id": str, "dataset": str}, ...]
#              (empty list if the runs dataset doesn't exist yet — a fresh
#              host with no runs ever performed — that's a valid, checked
#              state, not "unavailable"); None only if the zfs(8) binary
#              itself could not be run at all
#   sideEffects: runs 'zfs list -H -o name -r <runs_ds>' with a bounded timeout
def _list_zfs_run_datasets(runs_ds: str) -> list[dict] | None:
    """Return {"run_id","dataset"} entries under runs_ds, or None if zfs(8) is unavailable."""
    try:
        proc = subprocess.run(
            ["zfs", "list", "-H", "-o", "name", "-r", runs_ds],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("gc: zfs unavailable: %s", exc)
        return None
    if proc.returncode != 0:
        # Most commonly: the runs dataset doesn't exist yet (no run has ever
        # happened on this host/pool) — a genuinely empty, checked state.
        log.debug("gc: zfs list %s exited %d: %s", runs_ds, proc.returncode, proc.stderr.strip())
        return []
    entries: list[dict] = []
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name or name == runs_ds:
            continue  # skip the parent dataset entry itself
        run_id = name.rsplit("/", 1)[-1]
        entries.append({"run_id": run_id, "dataset": name})
    return entries
# _list_zfs_run_datasets:end


# _list_plaindir_run_datasets:start
#   purpose: enumerate plaindir run copies under the store's runs directory
#   input:
#     runs_dir: str — absolute directory path, e.g. '/var/jailrun/runs'
#   output:
#     entries: list[dict] | None — [{"run_id": str, "dataset": str}, ...]
#              (empty list if runs_dir doesn't exist yet — valid, checked
#              state); None only on a genuine enumeration failure (e.g.
#              permission denied)
#   sideEffects: calls os.listdir(runs_dir) / os.path.isdir() only
def _list_plaindir_run_datasets(runs_dir: str) -> list[dict] | None:
    """Return {"run_id","dataset"} entries under runs_dir, or None on genuine failure."""
    try:
        names = os.listdir(runs_dir)
    except FileNotFoundError:
        return []  # no runs directory yet — nothing to report, not an error
    except OSError as exc:
        log.debug("gc: could not list %s: %s", runs_dir, exc)
        return None
    entries: list[dict] = []
    for run_id in names:
        full = os.path.join(runs_dir, run_id)
        if os.path.isdir(full):
            entries.append({"run_id": run_id, "dataset": full})
    return entries
# _list_plaindir_run_datasets:end


# collect:start
#   purpose: gather real jls/rundb/dataset snapshots from this host, best-effort
#   input: none
#   output:
#     tuple[list[str] | None, list[dict] | None, list[dict] | None, list[str]]
#     — (known_jails, rundb_rows, known_datasets, notes); any of the first
#     three is None when that source could not be read on this host; notes
#     carries a human-readable reason for each None, so `jailrun gc` degrades
#     to reporting "couldn't enumerate X on this host" instead of crashing or
#     silently guessing empty
#   sideEffects: runs 'jls -n name'; imports and calls
#                runtime.rundb.RunDB().list_runs(status=None) (may open/create
#                the sqlite db at JAILRUN_DB); runs 'zfs list' or lists a
#                plaindir directory tree, depending on JAILRUN_STORE_BACKEND
def collect() -> tuple[list[str] | None, list[dict] | None, list[dict] | None, list[str]]:
    """Gather real jls/rundb/dataset state from this host. Never raises."""
    notes: list[str] = []

    known_jails = _list_jail_names()
    if known_jails is None:
        notes.append(
            "could not enumerate jails via `jls -n name` on this host "
            "(not FreeBSD, or jls unavailable) — skipping jail-related checks"
        )

    try:
        from runtime.rundb import RunDB  # noqa: PLC0415  (lazy import, same pattern as engine.py/cli.py)
        rundb_rows: list[dict] | None = RunDB().list_runs(status=None)
    except Exception as exc:  # noqa: BLE001 — list_runs() may raise OSError/sqlite3.Error by design (see rundb.py)
        rundb_rows = None
        notes.append(f"could not read the rundb ({exc}) — skipping rundb-dependent checks")

    backend = _get_backend()
    if backend == "zfs":
        runs_ds = f"{_get_zpool()}/runs"
        known_datasets = _list_zfs_run_datasets(runs_ds)
    else:
        runs_dir = os.path.join(_get_mountpoint_base(), "runs")
        known_datasets = _list_plaindir_run_datasets(runs_dir)
    if known_datasets is None:
        notes.append(
            f"could not enumerate run datasets (backend={backend!r}) on this host "
            "— skipping dataset checks"
        )

    return known_jails, rundb_rows, known_datasets, notes
# collect:end


# ---------------------------------------------------------------------------
# --fix: apply cleanup actions, defensively (one failure never stops the rest)
# ---------------------------------------------------------------------------

# _destroy_dataset:start
#   purpose: destroy one orphaned dataset/directory, matching the destroy
#            mechanism store.py's own Store.destroy() uses per backend
#   input:
#     dataset: str — full ZFS dataset name (zfs backend) or absolute
#              directory path (plaindir backend)
#   output:
#     tuple[bool, str] — (ok, detail message)
#   sideEffects: zfs backend: runs 'zfs destroy <dataset>', retried once with
#                '-f' if the plain destroy fails (mirrors store.py's destroy()
#                "busy right after jail -r" tolerance); plaindir backend:
#                shutil.rmtree(dataset)
def _destroy_dataset(dataset: str) -> tuple[bool, str]:
    """Destroy one orphaned dataset/directory. Never raises."""
    if _get_backend() == "zfs":
        rc, _out, err = _run_ok(["zfs", "destroy", dataset])
        if rc == 0:
            return True, "zfs destroy succeeded"
        # One forced retry — mirrors store.py's destroy() tolerance for the
        # brief "dataset is busy" window right after a jail is removed.
        rc2, _out2, err2 = _run_ok(["zfs", "destroy", "-f", dataset])
        if rc2 == 0:
            return True, "zfs destroy -f succeeded (after busy retry)"
        return False, f"zfs destroy failed (rc={rc}): {err.strip() or err2.strip()}"
    try:
        shutil.rmtree(dataset)
        return True, "rm -rf succeeded"
    except OSError as exc:
        return False, f"rm -rf failed: {exc}"
# _destroy_dataset:end


# apply_fixes:start
#   purpose: actually clean up every detected Orphan, defensively
#   input:
#     orphans: list[Orphan] — output of reconcile()
#   output:
#     outcomes: list[FixOutcome] — one outcome per orphan, same order
#   sideEffects: stale_rundb_row: imports runtime.rundb.RunDB and calls
#                .record_exit(identifier, 'killed', None); orphaned_jail:
#                runs 'jail -r <identifier>'; orphaned_dataset: destroys the
#                dataset/directory (see _destroy_dataset). Each item is
#                wrapped in its own try/except — one failure never stops the
#                rest from being attempted (per-item defense in depth, same
#                spirit as engine.py's teardown block).
def apply_fixes(orphans: list[Orphan]) -> list[FixOutcome]:
    """Clean up every Orphan; one failure never stops the rest. Never raises."""
    outcomes: list[FixOutcome] = []
    for o in orphans:
        try:
            if o.kind == "stale_rundb_row":
                from runtime.rundb import RunDB  # noqa: PLC0415
                RunDB().record_exit(o.identifier, "killed", None)
                outcomes.append(FixOutcome(o.kind, o.identifier, True, "marked 'killed' in rundb"))
            elif o.kind == "orphaned_jail":
                rc, _out, err = _run_ok(["jail", "-r", o.identifier])
                if rc == 0:
                    outcomes.append(FixOutcome(o.kind, o.identifier, True, "jail -r succeeded"))
                else:
                    outcomes.append(FixOutcome(
                        o.kind, o.identifier, False, f"jail -r failed (rc={rc}): {err.strip()}"
                    ))
            elif o.kind == "orphaned_dataset":
                ok, detail = _destroy_dataset(o.identifier)
                outcomes.append(FixOutcome(o.kind, o.identifier, ok, detail))
            else:
                # Unknown kind — should never happen (reconcile() only emits
                # the three kinds above), but never crash the fix loop over it.
                outcomes.append(FixOutcome(o.kind, o.identifier, False, f"unknown orphan kind {o.kind!r}"))
        except Exception as exc:  # noqa: BLE001 — one item's failure must never abort the rest
            log.warning("gc --fix: unexpected error cleaning up %s %r: %s", o.kind, o.identifier, exc)
            outcomes.append(FixOutcome(o.kind, o.identifier, False, f"unexpected error: {exc}"))
    return outcomes
# apply_fixes:end


# ---------------------------------------------------------------------------
# Top-level entry point, rendering, exit code
# ---------------------------------------------------------------------------

# run_gc:start
#   purpose: run the full gc cycle — gather real state, reconcile, optionally fix
#   input:
#     fix: bool — when True, actually clean up detected orphans
#   output:
#     tuple[list[Orphan], list[FixOutcome] | None, list[str]] — (orphans,
#     fixes, notes); fixes is None in dry-run mode (fix=False), a list (one
#     entry per orphan, possibly empty) when fix=True
#   sideEffects: calls collect() (real jls/rundb/zfs I/O) and, iff fix=True,
#                apply_fixes() (real jail -r / rundb / zfs destroy I/O)
def run_gc(fix: bool = False) -> tuple[list[Orphan], list[FixOutcome] | None, list[str]]:
    """Gather state, reconcile, and (if fix=True) clean up. Never raises."""
    known_jails, rundb_rows, known_datasets, notes = collect()
    orphans = reconcile(known_jails, rundb_rows, known_datasets)
    fixes = apply_fixes(orphans) if fix else None
    return orphans, fixes, notes
# run_gc:end


# render:start
#   purpose: format a gc report as text or JSON (mirrors runtime/doctor.py's render())
#   input:
#     orphans: list[Orphan] — detected orphans
#     fixes: list[FixOutcome] | None — per-orphan fix outcomes (None = dry run)
#     notes: list[str] — diagnostic notes (e.g. "couldn't enumerate X")
#     fmt: str — 'text' (default) or 'json'
#   output:
#     formatted: str
#   sideEffects: none (pure formatting)
def render(
    orphans: list[Orphan],
    fixes: list[FixOutcome] | None,
    notes: list[str],
    fmt: str = "text",
) -> str:
    """Format a gc report as text or JSON."""
    if fmt == "json":
        data = {
            "orphans": [
                {"kind": o.kind, "identifier": o.identifier, "detail": o.detail, "fix_action": o.fix_action}
                for o in orphans
            ],
            "fixes": (
                None if fixes is None else
                [{"kind": f.kind, "identifier": f.identifier, "ok": f.ok, "detail": f.detail} for f in fixes]
            ),
            "notes": list(notes),
        }
        return json.dumps(data, indent=2)

    lines: list[str] = []
    for n in notes:
        lines.append(f"[NOTE] {n}")

    if not orphans:
        lines.append("no orphans found")
    else:
        fixes_by_index = fixes if fixes is not None else [None] * len(orphans)
        for o, f in zip(orphans, fixes_by_index):
            lines.append(f"[ORPHAN] {o.kind}: {o.identifier} — {o.detail}")
            lines.append(f"    Fix: {o.fix_action}")
            if f is not None:
                marker = "[FIXED]" if f.ok else "[FIX-FAILED]"
                lines.append(f"    {marker} {f.detail}")

    return "\n".join(lines)
# render:end


# exit_code_for:start
#   purpose: compute the process exit code for a gc report
#   input:
#     orphans: list[Orphan] — detected orphans
#     fixes: list[FixOutcome] | None — per-orphan fix outcomes (None = dry run)
#   output:
#     exit_code: int — dry run (fixes is None): 0 if orphans is empty, 1
#                otherwise (health-check semantics). --fix mode: 0 if every
#                fix succeeded (or there was nothing to fix), 1 if any single
#                fix action failed.
#   sideEffects: none (pure computation)
def exit_code_for(orphans: list[Orphan], fixes: list[FixOutcome] | None) -> int:
    """0 = clean (or all fixes succeeded); 1 = orphans found (or a fix failed)."""
    if fixes is None:
        return 1 if orphans else 0
    return 1 if any(not f.ok for f in fixes) else 0
# exit_code_for:end

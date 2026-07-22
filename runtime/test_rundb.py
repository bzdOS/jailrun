#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_rundb.py
# PURPOSE: unit tests for runtime/rundb.py (RunDB) and runtime/cli.py's ps table renderer
# INTENT: verify record_start/record_exit/list_runs semantics and graceful-degrade
#         behavior against throwaway :memory:/tmp-file dbs — NEVER the real
#         /var/db/jailrun/runs.db
# DEPENDENCIES: stdlib (os, sqlite3, tempfile); runtime.rundb (RunDB, DEFAULT_DB_PATH);
#               runtime.cli (_render_ps_table)
# PUBLIC_API: each test_* function is callable by pytest; run_all() for direct execution
# END_AI_HEADER

"""
test_rundb.py — unit tests for runtime/rundb.py and the `jailrun ps` table renderer.

Every test uses RunDB(path=":memory:") or a tmp-file path — never the real
default (/var/db/jailrun/runs.db, or whatever JAILRUN_DB happens to be set to
in the environment this runs in).

Run with pytest:
    python3 -m pytest runtime/test_rundb.py -v
"""

import os
import sqlite3
import tempfile

from runtime.rundb import RunDB, DEFAULT_DB_PATH
from runtime.cli import _render_ps_table


# ---------------------------------------------------------------------------
# record_start / list_runs
# ---------------------------------------------------------------------------

# CONTRACT: record_start(jail_name, ...) -> list_runs() contains exactly one row
# for jail_name with status 'running', exit_code None, and the given image/dataset.
def test_record_start_then_list_runs_shows_running():
    """record_start() makes the jail show up in list_runs() as 'running'."""
    db = RunDB(path=":memory:")
    db.record_start("jailrun-abc123", "alpine:3.19", "sha256:deadbeef", "jailrun/runs/abc123")

    rows = db.list_runs()
    assert len(rows) == 1
    row = rows[0]
    assert row["jail_name"] == "jailrun-abc123"
    assert row["image"] == "alpine:3.19"
    assert row["image_digest"] == "sha256:deadbeef"
    assert row["dataset"] == "jailrun/runs/abc123"
    assert row["status"] == "running"
    assert row["exit_code"] is None
    assert row["started_at"]  # non-empty timestamp
    assert row["ended_at"] is None
    print("PASS test_record_start_then_list_runs_shows_running")


# ---------------------------------------------------------------------------
# record_exit
# ---------------------------------------------------------------------------

# CONTRACT: record_exit(jail_name, 'exited', 0) flips a running row's status +
# exit_code and sets ended_at, without touching image/dataset/started_at.
def test_record_exit_flips_status_and_exit_code():
    """record_exit() updates status/exit_code/ended_at on an existing row."""
    db = RunDB(path=":memory:")
    db.record_start("jailrun-xyz", "esphome/esphome:2025.5", "", "jailrun/runs/xyz")
    before = db.list_runs()[0]
    assert before["status"] == "running"

    db.record_exit("jailrun-xyz", "exited", 0)

    after = db.list_runs()[0]
    assert after["status"] == "exited"
    assert after["exit_code"] == 0
    assert after["ended_at"]  # now set
    # unrelated columns unchanged
    assert after["jail_name"] == "jailrun-xyz"
    assert after["image"] == "esphome/esphome:2025.5"
    assert after["started_at"] == before["started_at"]
    print("PASS test_record_exit_flips_status_and_exit_code")


# CONTRACT: record_exit() with a nonzero exit_code and status='killed' is
# recorded faithfully (covers the SIGKILL / rctl-deny path).
def test_record_exit_killed_with_nonzero_code():
    """record_exit() records 'killed' status and a nonzero exit_code."""
    db = RunDB(path=":memory:")
    db.record_start("jailrun-killme", "alpine:3.19", "", "jailrun/runs/killme")
    db.record_exit("jailrun-killme", "killed", 137)

    row = db.list_runs()[0]
    assert row["status"] == "killed"
    assert row["exit_code"] == 137
    print("PASS test_record_exit_killed_with_nonzero_code")


# CONTRACT: record_exit() for a jail_name with no prior record_start() row is a
# harmless no-op (0 rows affected) — list_runs() stays empty, no exception.
def test_record_exit_without_prior_start_is_noop():
    """record_exit() on an unknown jail_name doesn't raise or create a row."""
    db = RunDB(path=":memory:")
    db.record_exit("jailrun-never-started", "exited", 0)
    assert db.list_runs() == []
    print("PASS test_record_exit_without_prior_start_is_noop")


# ---------------------------------------------------------------------------
# list_runs(status=...) filtering + ordering
# ---------------------------------------------------------------------------

# CONTRACT: list_runs(status='running') returns only running rows;
# list_runs(status='exited') returns only exited rows; list_runs() (no filter)
# returns all of them, newest-started first.
def test_list_runs_status_filter():
    """list_runs(status=...) filters correctly; unfiltered returns everything."""
    db = RunDB(path=":memory:")
    db.record_start("jailrun-one", "alpine:3.19", "", "jailrun/runs/one")
    db.record_start("jailrun-two", "alpine:3.19", "", "jailrun/runs/two")
    db.record_exit("jailrun-two", "exited", 0)
    db.record_start("jailrun-three", "alpine:3.19", "", "jailrun/runs/three")
    db.record_exit("jailrun-three", "killed", 9)

    running = db.list_runs(status="running")
    assert {r["jail_name"] for r in running} == {"jailrun-one"}

    exited = db.list_runs(status="exited")
    assert {r["jail_name"] for r in exited} == {"jailrun-two"}

    killed = db.list_runs(status="killed")
    assert {r["jail_name"] for r in killed} == {"jailrun-three"}

    everything = db.list_runs()
    assert {r["jail_name"] for r in everything} == {"jailrun-one", "jailrun-two", "jailrun-three"}
    print("PASS test_list_runs_status_filter")


# CONTRACT: list_runs() is newest-started first (ORDER BY started_at DESC).
def test_list_runs_newest_first():
    """list_runs() orders rows by started_at descending."""
    db = RunDB(path=":memory:")
    # Force distinct, monotonically increasing started_at values directly —
    # avoids depending on real wall-clock granularity/sleeps in a unit test.
    db._get_conn().execute(
        "INSERT INTO runs (jail_name, image, status, started_at) "
        "VALUES ('jailrun-old', 'alpine:3.19', 'exited', '2026-01-01T00:00:00+00:00')"
    )
    db._get_conn().execute(
        "INSERT INTO runs (jail_name, image, status, started_at) "
        "VALUES ('jailrun-new', 'alpine:3.19', 'running', '2026-06-01T00:00:00+00:00')"
    )
    db._get_conn().commit()

    rows = db.list_runs()
    assert [r["jail_name"] for r in rows] == ["jailrun-new", "jailrun-old"]
    print("PASS test_list_runs_newest_first")


# CONTRACT: record_start() on an already-present jail_name replaces the row
# wholesale (fresh 'running' state) — a rerun of the same jail name.
def test_record_start_replaces_existing_row():
    """record_start() on an existing jail_name resets it to a fresh 'running' row."""
    db = RunDB(path=":memory:")
    db.record_start("jailrun-rerun", "alpine:3.18", "", "jailrun/runs/rerun-1")
    db.record_exit("jailrun-rerun", "exited", 1)
    assert db.list_runs()[0]["status"] == "exited"

    db.record_start("jailrun-rerun", "alpine:3.19", "", "jailrun/runs/rerun-2")

    rows = db.list_runs()
    assert len(rows) == 1
    assert rows[0]["status"] == "running"
    assert rows[0]["image"] == "alpine:3.19"
    assert rows[0]["dataset"] == "jailrun/runs/rerun-2"
    assert rows[0]["exit_code"] is None
    assert rows[0]["ended_at"] is None
    print("PASS test_record_start_replaces_existing_row")


# ---------------------------------------------------------------------------
# Graceful degrade (record_start / record_exit only — see rundb.py invariants)
# ---------------------------------------------------------------------------

# CONTRACT: record_start()/record_exit() against an unwritable/unusable db path
# log a warning and return None — they must NEVER raise.
def test_record_calls_degrade_gracefully_on_unwritable_path():
    """record_start()/record_exit() swallow errors from a bad db path."""
    with tempfile.TemporaryDirectory() as tmp:
        # A path whose "parent directory" is actually a plain file: os.makedirs()
        # on it must fail (NotADirectoryError, a subclass of OSError).
        blocker_file = os.path.join(tmp, "not_a_directory")
        with open(blocker_file, "w", encoding="utf-8") as fh:
            fh.write("x")
        bad_path = os.path.join(blocker_file, "nested", "runs.db")

        db = RunDB(path=bad_path)
        # Must not raise.
        db.record_start("jailrun-nope", "alpine:3.19", "", "jailrun/runs/nope")
        db.record_exit("jailrun-nope", "exited", 0)
    print("PASS test_record_calls_degrade_gracefully_on_unwritable_path")


# CONTRACT: list_runs() against the same unwritable path is allowed to raise
# (it does not swallow errors, per rundb.py's documented invariant) — the CLI
# layer (runtime/cli.py's _cmd_ps) is what turns this into a friendly empty list.
def test_list_runs_may_raise_on_unusable_path():
    """list_runs() propagates OSError from an unusable db path (by design)."""
    with tempfile.TemporaryDirectory() as tmp:
        blocker_file = os.path.join(tmp, "not_a_directory")
        with open(blocker_file, "w", encoding="utf-8") as fh:
            fh.write("x")
        bad_path = os.path.join(blocker_file, "nested", "runs.db")

        db = RunDB(path=bad_path)
        raised = False
        try:
            db.list_runs()
        except (OSError, sqlite3.Error):
            raised = True
        assert raised, "list_runs() should propagate the underlying OSError"
    print("PASS test_list_runs_may_raise_on_unusable_path")


# CONTRACT: a RunDB backed by a real tmp file persists across separate RunDB
# instances pointed at the same path (unlike ':memory:', which does not).
def test_tmp_file_path_persists_across_instances():
    """A file-backed RunDB persists writes across separate instances at the same path."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "sub", "runs.db")

        db1 = RunDB(path=db_path)
        db1.record_start("jailrun-persist", "alpine:3.19", "", "jailrun/runs/persist")

        db2 = RunDB(path=db_path)
        rows = db2.list_runs()
        assert len(rows) == 1
        assert rows[0]["jail_name"] == "jailrun-persist"
    print("PASS test_tmp_file_path_persists_across_instances")


# CONTRACT: RunDB() with no path arg and no JAILRUN_DB env var resolves to
# DEFAULT_DB_PATH (pure path-resolution check — does not touch the filesystem).
def test_default_path_resolution():
    """RunDB() resolves to DEFAULT_DB_PATH when no arg/env var is given."""
    saved = os.environ.pop("JAILRUN_DB", None)
    try:
        db = RunDB()
        assert db.path == DEFAULT_DB_PATH
    finally:
        if saved is not None:
            os.environ["JAILRUN_DB"] = saved
    print("PASS test_default_path_resolution")


# CONTRACT: RunDB() with no path arg picks up JAILRUN_DB from the environment.
def test_env_var_overrides_default_path():
    """RunDB() with no explicit path uses JAILRUN_DB when set."""
    saved = os.environ.get("JAILRUN_DB")
    os.environ["JAILRUN_DB"] = ":memory:"
    try:
        db = RunDB()
        assert db.path == ":memory:"
    finally:
        if saved is None:
            os.environ.pop("JAILRUN_DB", None)
        else:
            os.environ["JAILRUN_DB"] = saved
    print("PASS test_env_var_overrides_default_path")


# CONTRACT: an explicit constructor path arg wins over JAILRUN_DB.
def test_constructor_arg_overrides_env_var():
    """RunDB(path=...) wins over JAILRUN_DB when both are given."""
    saved = os.environ.get("JAILRUN_DB")
    os.environ["JAILRUN_DB"] = "/var/db/jailrun/runs.db"
    try:
        db = RunDB(path=":memory:")
        assert db.path == ":memory:"
    finally:
        if saved is None:
            os.environ.pop("JAILRUN_DB", None)
        else:
            os.environ["JAILRUN_DB"] = saved
    print("PASS test_constructor_arg_overrides_env_var")


# ---------------------------------------------------------------------------
# log_path column: record_start(log_path=...) / get_log_path()
# ---------------------------------------------------------------------------

# CONTRACT: record_start(..., log_path=...) stores it, and it round-trips
# through both list_runs() and get_log_path().
def test_record_start_with_log_path_stored_and_retrieved():
    """record_start(log_path=...) is stored and retrievable via get_log_path()."""
    db = RunDB(path=":memory:")
    db.record_start(
        "jailrun-haslog",
        "alpine:3.19",
        "sha256:deadbeef",
        "jailrun/runs/haslog",
        log_path="/var/log/jailrun/jailrun-haslog.log",
    )

    row = db.list_runs()[0]
    assert row["log_path"] == "/var/log/jailrun/jailrun-haslog.log"
    assert db.get_log_path("jailrun-haslog") == "/var/log/jailrun/jailrun-haslog.log"
    print("PASS test_record_start_with_log_path_stored_and_retrieved")


# CONTRACT: record_start() without a log_path arg (the default, matching
# every EXISTING caller/test) stores NULL — get_log_path() returns None, and
# nothing breaks (backward compatibility with the pre-log_path signature).
def test_record_start_without_log_path_defaults_to_null():
    """record_start() with no log_path arg stores NULL; get_log_path() -> None."""
    db = RunDB(path=":memory:")
    db.record_start("jailrun-nolog", "alpine:3.19", "", "jailrun/runs/nolog")

    row = db.list_runs()[0]
    assert row["log_path"] is None
    assert db.get_log_path("jailrun-nolog") is None
    print("PASS test_record_start_without_log_path_defaults_to_null")


# CONTRACT: get_log_path() on a jail_name with no row at all returns None
# (not an exception).
def test_get_log_path_unknown_jail_returns_none():
    """get_log_path() on an unrecognized jail_name returns None."""
    db = RunDB(path=":memory:")
    assert db.get_log_path("jailrun-never-existed") is None
    print("PASS test_get_log_path_unknown_jail_returns_none")


# CONTRACT: get_log_path() on a jail_name whose row exists but has log_path=NULL
# also returns None — same shape as "not found", not a crash.
def test_get_log_path_found_row_but_null_log_path():
    """get_log_path() returns None for a real row whose log_path column is NULL."""
    db = RunDB(path=":memory:")
    db.record_start("jailrun-rowbutnolog", "alpine:3.19", "", "jailrun/runs/rowbutnolog")
    assert db.get_log_path("jailrun-rowbutnolog") is None
    print("PASS test_get_log_path_found_row_but_null_log_path")


# ---------------------------------------------------------------------------
# Schema migration: ALTER TABLE ... ADD COLUMN log_path (idempotent upgrade)
# ---------------------------------------------------------------------------

# CONTRACT: opening the SAME on-disk db file via separate RunDB instances
# (each running its own _get_conn() schema/migration step on a fresh
# sqlite3 connection) never raises on the "column already exists" path —
# proves the ALTER TABLE migration is idempotent, not a one-shot fluke.
def test_schema_migration_idempotent_across_instances():
    """Re-opening the same db file in fresh RunDB instances never errors on the migration."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "runs.db")

        db1 = RunDB(path=db_path)
        db1.record_start(
            "jailrun-mig",
            "alpine:3.19",
            "",
            "jailrun/runs/mig",
            log_path="/var/log/jailrun/jailrun-mig.log",
        )

        # A second, completely separate instance/connection against the SAME
        # db file must not raise on the ALTER TABLE ADD COLUMN log_path step.
        db2 = RunDB(path=db_path)
        rows = db2.list_runs()
        assert len(rows) == 1
        assert rows[0]["log_path"] == "/var/log/jailrun/jailrun-mig.log"

        # And a third, for good measure (not a fluke of exactly two).
        db3 = RunDB(path=db_path)
        assert db3.get_log_path("jailrun-mig") == "/var/log/jailrun/jailrun-mig.log"
    print("PASS test_schema_migration_idempotent_across_instances")


# CONTRACT: a db file created with the OLD (pre-log_path) table shape upgrades
# in place the first time RunDB opens it — no fresh db needed, pre-existing
# rows survive with log_path=NULL, and it's fully usable (including writing a
# NEW row with a real log_path) afterward.
def test_alter_table_upgrades_pre_existing_old_shape_db():
    """A pre-log_path-column db file is upgraded in place by RunDB's migration."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "old_runs.db")

        # Simulate a db created before the log_path column existed at all.
        old_conn = sqlite3.connect(db_path)
        old_conn.execute(
            """
            CREATE TABLE runs (
                jail_name    TEXT PRIMARY KEY,
                image        TEXT,
                image_digest TEXT,
                dataset      TEXT,
                status       TEXT CHECK(status IN ('running', 'exited', 'killed')),
                exit_code    INTEGER,
                started_at   TEXT,
                ended_at     TEXT
            )
            """
        )
        old_conn.execute(
            "INSERT INTO runs (jail_name, image, status, started_at) "
            "VALUES ('jailrun-old-shape', 'alpine:3.19', 'exited', '2026-01-01T00:00:00+00:00')"
        )
        old_conn.commit()
        old_conn.close()

        # Opening this pre-existing db via RunDB must upgrade it in place.
        db = RunDB(path=db_path)
        rows = db.list_runs()
        assert len(rows) == 1
        assert rows[0]["jail_name"] == "jailrun-old-shape"
        assert rows[0]["log_path"] is None
        assert db.get_log_path("jailrun-old-shape") is None

        # And it's fully usable going forward, exactly like a fresh db.
        db.record_start(
            "jailrun-new-shape",
            "alpine:3.19",
            "",
            "jailrun/runs/new-shape",
            log_path="/var/log/jailrun/jailrun-new-shape.log",
        )
        assert db.get_log_path("jailrun-new-shape") == "/var/log/jailrun/jailrun-new-shape.log"
    print("PASS test_alter_table_upgrades_pre_existing_old_shape_db")


# ---------------------------------------------------------------------------
# _render_ps_table (runtime/cli.py) — pure rendering, shape only
# ---------------------------------------------------------------------------

# CONTRACT: _render_ps_table([]) renders just the header line, no data rows.
def test_render_ps_table_empty():
    """Empty run list renders header-only output."""
    output = _render_ps_table([])
    lines = output.splitlines()
    assert len(lines) == 1
    for col in ("JAIL", "IMAGE", "STATUS", "STARTED"):
        assert col in lines[0]
    print("PASS test_render_ps_table_empty")


# CONTRACT: _render_ps_table(rows) renders one line per row plus the header,
# with each row's field values present in its line.
def test_render_ps_table_with_rows():
    """Non-empty run list renders header + one line per row, values present."""
    rows = [
        {
            "jail_name": "jailrun-abc123",
            "image": "alpine:3.19",
            "status": "running",
            "started_at": "2026-07-22T12:00:00+00:00",
        },
        {
            "jail_name": "jailrun-def456",
            "image": "esphome/esphome:2025.5",
            "status": "exited",
            "started_at": "2026-07-21T09:00:00+00:00",
        },
    ]
    output = _render_ps_table(rows)
    lines = output.splitlines()
    assert len(lines) == 3  # header + 2 rows
    assert "jailrun-abc123" in lines[1]
    assert "alpine:3.19" in lines[1]
    assert "running" in lines[1]
    assert "jailrun-def456" in lines[2]
    assert "esphome/esphome:2025.5" in lines[2]
    assert "exited" in lines[2]
    print("PASS test_render_ps_table_with_rows")


# CONTRACT: _render_ps_table tolerates rows missing keys (renders "" for them)
# rather than raising KeyError.
def test_render_ps_table_tolerates_missing_keys():
    """Missing dict keys render as empty strings, no KeyError."""
    output = _render_ps_table([{"jail_name": "jailrun-partial"}])
    lines = output.splitlines()
    assert len(lines) == 2
    assert "jailrun-partial" in lines[1]
    print("PASS test_render_ps_table_tolerates_missing_keys")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_record_start_then_list_runs_shows_running,
    test_record_exit_flips_status_and_exit_code,
    test_record_exit_killed_with_nonzero_code,
    test_record_exit_without_prior_start_is_noop,
    test_list_runs_status_filter,
    test_list_runs_newest_first,
    test_record_start_replaces_existing_row,
    test_record_calls_degrade_gracefully_on_unwritable_path,
    test_list_runs_may_raise_on_unusable_path,
    test_tmp_file_path_persists_across_instances,
    test_default_path_resolution,
    test_env_var_overrides_default_path,
    test_constructor_arg_overrides_env_var,
    test_record_start_with_log_path_stored_and_retrieved,
    test_record_start_without_log_path_defaults_to_null,
    test_get_log_path_unknown_jail_returns_none,
    test_get_log_path_found_row_but_null_log_path,
    test_schema_migration_idempotent_across_instances,
    test_alter_table_upgrades_pre_existing_old_shape_db,
    test_render_ps_table_empty,
    test_render_ps_table_with_rows,
    test_render_ps_table_tolerates_missing_keys,
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

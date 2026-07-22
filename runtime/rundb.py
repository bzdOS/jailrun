#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/rundb.py
# PURPOSE: sqlite-backed run-state store — records every jailrun invocation so
#          `jailrun ps` can list what has run/is running
# INTENT: foundation for ps/logs/gc/recovery; a tiny persistent ledger of jail
#         runs, independent of engine.py so it can land without touching the
#         in-flight engine.py track (see NOTE below)
# DEPENDENCIES: stdlib only (sqlite3, os, logging, datetime)
# PUBLIC_API: RunDB (class) — record_start(), record_exit(), list_runs();
#             DEFAULT_DB_PATH (str constant)
# END_AI_HEADER

# START_INVARIANTS
# - record_start()/record_exit() never raise: any OSError/sqlite3.Error opening
#   or writing the db is caught, logged as a warning, and swallowed — recording
#   run state must never crash an actual jail run.
# - list_runs() does NOT swallow errors: an unreadable/unwritable db path is a
#   genuine environment/programmer problem on a read path and propagates to the
#   caller. Callers that need a friendly empty listing (e.g. `jailrun ps`) catch
#   it there instead.
# - jail_name is the primary key: record_start() on a name that already has a
#   row REPLACES it wholesale (a rerun of the same jail name starts a fresh run
#   row — status/exit_code/ended_at reset).
# - ':memory:' is supported for tests, but the in-memory database lives on a
#   single sqlite3 connection: a new RunDB(path=':memory:') is a new, empty
#   database. Tests must reuse one RunDB instance across calls to see their
#   own writes.
# END_INVARIANTS

"""
runtime/rundb.py — sqlite-backed run-state store for jailrun.

Records every `jailrun run` invocation (start + exit) into a small sqlite
database so `jailrun ps` has something real to list, and so later work
(logs/gc/recovery) has a ledger of jail runs to build on.

NOTE (deferred, follow-up task): engine.py will call RunDB().record_start()/
record_exit() around the actual jail lifecycle (jail -c / jexec / jail -r)
once this lands. That wiring is intentionally NOT done here — engine.py is
being modified on another track right now, and touching it here would risk a
mid-flight merge conflict / lost work. For now this module + `jailrun ps` +
its tests stand alone; the db simply stays empty until the follow-up wires
engine.py up.

DB path resolution (RunDB.__init__):
  1. explicit `path=` constructor arg, if given (tests use this, e.g. ':memory:'
     or a tmp file — NEVER the real default path).
  2. else the JAILRUN_DB environment variable.
  3. else DEFAULT_DB_PATH ('/var/db/jailrun/runs.db').

Schema (single table, lazily created on first use):
  runs(jail_name TEXT PRIMARY KEY, image TEXT, image_digest TEXT, dataset TEXT,
       status TEXT CHECK(status IN ('running','exited','killed')),
       exit_code INTEGER, started_at TEXT, ended_at TEXT)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger("jailrun.rundb")

DEFAULT_DB_PATH = "/var/db/jailrun/runs.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
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


# _now_iso:start
#   purpose: produce a sortable UTC timestamp string for started_at/ended_at columns
#   input: none
#   output: iso: str — UTC ISO-8601 timestamp, seconds precision (e.g. '2026-07-22T12:34:56+00:00')
#   sideEffects: none (reads the wall clock only)
def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (sortable, seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
# _now_iso:end


# RunDB:start
#   purpose: sqlite-backed store of jailrun run state, one row per jail_name
#   input:
#     path: str | None — db file path, or ':memory:'. None (default) means:
#           use the JAILRUN_DB env var, falling back to DEFAULT_DB_PATH.
#   output: a RunDB instance; no I/O happens until the first record_*/list_runs call
#   sideEffects: none at construction time (path resolution only, lazy open — see _get_conn)
class RunDB:
    """sqlite-backed run-state store. See module docstring for the schema."""

    # __init__:start
    #   purpose: resolve the db path (constructor arg > JAILRUN_DB env var > default)
    #   input: path: str | None — see class docstring
    #   output: None
    #   sideEffects: none — no filesystem/db access happens here (lazy, see _get_conn)
    def __init__(self, path: str | None = None) -> None:
        self.path = path if path is not None else os.environ.get("JAILRUN_DB", DEFAULT_DB_PATH)
        self._conn: sqlite3.Connection | None = None
    # __init__:end

    # _get_conn:start
    #   purpose: return the lazily-opened, cached sqlite3 connection for this
    #            instance, creating the parent directory and the schema on
    #            first use
    #   input: none (uses self.path)
    #   output: conn: sqlite3.Connection — open connection, row_factory=sqlite3.Row
    #   sideEffects: on first call only — os.makedirs(parent dir, exist_ok=True)
    #                unless self.path == ':memory:'; sqlite3.connect(self.path);
    #                CREATE TABLE IF NOT EXISTS runs (...); caches the connection
    #                on self._conn for reuse (required for ':memory:' to persist
    #                writes across calls on the same instance).
    #                Raises OSError/sqlite3.Error on failure (mkdir/connect/DDL) —
    #                callers decide whether to swallow it (see class invariants).
    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self.path != ":memory:":
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA_SQL)
        conn.commit()
        self._conn = conn
        return conn
    # _get_conn:end

    # record_start:start
    #   purpose: insert/replace a 'running' row for jail_name at run start
    #   input:
    #     jail_name: str — jail name, primary key (e.g. 'jailrun-<handle>')
    #     image: str — OCI image reference as given to `jailrun run`
    #     image_digest: str — resolved image digest (empty string if unknown)
    #     dataset: str — ZFS dataset / rootfs path backing this run
    #   output: None
    #   sideEffects: opens/creates the db (see _get_conn); INSERT OR REPLACE the
    #                row with status='running', exit_code=NULL, started_at=now,
    #                ended_at=NULL. On OSError/sqlite3.Error: logs a warning via
    #                log.warning() and returns — NEVER raises (recording run
    #                state must never crash an actual jail run).
    def record_start(self, jail_name: str, image: str, image_digest: str, dataset: str) -> None:
        """Record that jail_name has started running. Degrades gracefully on error."""
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(jail_name, image, image_digest, dataset, status, exit_code, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, 'running', NULL, ?, NULL)",
                (jail_name, image, image_digest, dataset, _now_iso()),
            )
            conn.commit()
        except (OSError, sqlite3.Error) as exc:
            log.warning("rundb: could not record start for %r: %s", jail_name, exc)
            return
    # record_start:end

    # record_exit:start
    #   purpose: flip an existing row to its terminal status at run exit
    #   input:
    #     jail_name: str — jail name, primary key
    #     status: str — 'exited' or 'killed' (schema CHECK also enforces this)
    #     exit_code: int — process exit code (or signal-derived code for 'killed')
    #   output: None
    #   sideEffects: opens/creates the db (see _get_conn); UPDATE runs SET
    #                status, exit_code, ended_at=now WHERE jail_name=?; a no-op
    #                (0 rows affected) if jail_name has no prior row. On
    #                OSError/sqlite3.Error: logs a warning and returns — NEVER
    #                raises.
    def record_exit(self, jail_name: str, status: str, exit_code: int) -> None:
        """Record that jail_name has exited. Degrades gracefully on error."""
        try:
            conn = self._get_conn()
            conn.execute(
                "UPDATE runs SET status = ?, exit_code = ?, ended_at = ? WHERE jail_name = ?",
                (status, exit_code, _now_iso(), jail_name),
            )
            conn.commit()
        except (OSError, sqlite3.Error) as exc:
            log.warning("rundb: could not record exit for %r: %s", jail_name, exc)
            return
    # record_exit:end

    # list_runs:start
    #   purpose: list recorded runs, newest first, optionally filtered by status
    #   input:
    #     status: str | None — one of 'running'/'exited'/'killed' to filter by,
    #             or None (default) for all runs
    #   output: rows: list[dict] — one dict per run (schema columns as keys),
    #           ordered by started_at descending (newest first)
    #   sideEffects: opens/creates the db (see _get_conn); read-only SELECT.
    #                Unlike record_start/record_exit, this does NOT swallow
    #                OSError/sqlite3.Error — an unreadable/unwritable db path
    #                here is a genuine problem on a read path; callers that
    #                want a friendly empty listing (e.g. `jailrun ps`) catch it.
    def list_runs(self, status: str | None = None) -> list[dict]:
        """List recorded runs, newest first. May raise OSError/sqlite3.Error."""
        conn = self._get_conn()
        if status is not None:
            cur = conn.execute(
                "SELECT * FROM runs WHERE status = ? ORDER BY started_at DESC",
                (status,),
            )
        else:
            cur = conn.execute("SELECT * FROM runs ORDER BY started_at DESC")
        return [dict(row) for row in cur.fetchall()]
    # list_runs:end
# RunDB:end

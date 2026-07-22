#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_logs.py
# PURPOSE: unit tests for runtime/cli.py's `jailrun logs` subcommand (_cmd_logs)
# INTENT: verify _cmd_logs's found/not-found/found-but-null-log_path/broken-db
#         behavior against a faked RunDB (patched at the module attribute the
#         lazy `from runtime.rundb import RunDB` import resolves against, same
#         pattern test_engine_rundb.py already uses) and a real temp log file
#         on disk for the found case
# DEPENDENCIES: stdlib (argparse, contextlib, io, os, sqlite3, tempfile); runtime.cli
#               (_cmd_logs); runtime.rundb (only its module attribute is patched —
#               the real class is never instantiated here)
# PUBLIC_API: each test_* function is callable by pytest; run_all() for direct execution
# END_AI_HEADER

"""
test_logs.py — unit tests for runtime/cli.py's `jailrun logs` subcommand.

Output is captured via contextlib.redirect_stdout/redirect_stderr into
io.StringIO (not pytest's capsys fixture), so every test here also runs
standalone via run_all() — matching this project's other test_*.py files,
none of which depend on pytest-only fixtures.

Run with pytest:
    python3 -m pytest runtime/test_logs.py -v
"""

import argparse
import contextlib
import io
import os
import sqlite3
import tempfile

import runtime.cli as cli
import runtime.rundb as rundb_module


# ---------------------------------------------------------------------------
# Fakes: RunDB (runtime.rundb.RunDB)
# ---------------------------------------------------------------------------

# _FakeRunDBWithLog:start
#   purpose: stand-in for runtime.rundb.RunDB whose get_log_path() returns a
#            fixed, pre-arranged mapping instead of touching sqlite
#   input (via the CLASS-level LOG_PATHS dict, set by each test before use):
#     LOG_PATHS: dict[str, str | None] — jail_name -> log_path (or None),
#                mirrors what a real RunDB.get_log_path() would return for a
#                known jail_name with/without a recorded log_path
#   sideEffects: none — pure in-memory lookup
class _FakeRunDBWithLog:
    LOG_PATHS: dict = {}

    def __init__(self, path=None) -> None:  # noqa: ARG002
        pass

    def get_log_path(self, jail_name: str) -> str | None:
        return _FakeRunDBWithLog.LOG_PATHS.get(jail_name)
# _FakeRunDBWithLog:end


# _BrokenRunDBForLogs:start
#   purpose: RunDB stand-in whose get_log_path() raises sqlite3.Error,
#            simulating an unreadable/unusable run-state db (see rundb.py's
#            own invariant: list_runs()/get_log_path() do NOT swallow errors
#            on the read path — _cmd_logs is what turns this into a friendly
#            CLI error, exactly like _cmd_ps already does for list_runs())
#   sideEffects: none of its own — raises instead of returning anything
class _BrokenRunDBForLogs:
    def __init__(self, path=None) -> None:  # noqa: ARG002
        pass

    def get_log_path(self, jail_name: str) -> str | None:  # noqa: ARG002
        raise sqlite3.OperationalError("simulated: database disk image is malformed")
# _BrokenRunDBForLogs:end


# ---------------------------------------------------------------------------
# Patch harness
# ---------------------------------------------------------------------------

# _patched_rundb:start
#   purpose: replace runtime.rundb.RunDB for the duration of a `with` block,
#            so _cmd_logs's lazy `from runtime.rundb import RunDB` resolves
#            to the fake instead of touching real sqlite
#   input: rundb_class: type — replaces runtime.rundb.RunDB
#   output: none (context manager)
#   sideEffects: mutates rundb_module.RunDB for the duration of the block;
#                ALWAYS restores the original in a finally, even on exception
@contextlib.contextmanager
def _patched_rundb(rundb_class):
    orig = rundb_module.RunDB
    rundb_module.RunDB = rundb_class
    try:
        yield
    finally:
        rundb_module.RunDB = orig
# _patched_rundb:end


def _args(jail: str) -> argparse.Namespace:
    """Build the minimal argparse.Namespace _cmd_logs() reads (args.jail)."""
    return argparse.Namespace(jail=jail)


def _run_cmd_logs(args: argparse.Namespace):
    """
    Invoke cli._cmd_logs(args) with stdout/stderr captured to StringIO.
    Returns (exit_code, stdout_text, stderr_text).
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli._cmd_logs(args)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# CONTRACT: found case — get_log_path() returns a real path to an existing
# file; _cmd_logs prints its exact contents to stdout and returns 0.
def test_logs_found_prints_content_and_returns_zero():
    """_cmd_logs prints the log file's contents and returns 0 when found."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = os.path.join(tmp, "jailrun-found.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("hello from inside the jail\nsecond line\n")

        _FakeRunDBWithLog.LOG_PATHS = {"jailrun-found": log_path}
        with _patched_rundb(_FakeRunDBWithLog):
            rc, out, err = _run_cmd_logs(_args("jailrun-found"))

    assert rc == 0
    assert out == "hello from inside the jail\nsecond line\n"
    assert err == ""
    print("PASS test_logs_found_prints_content_and_returns_zero")


# CONTRACT: not-found case — get_log_path() returns None because the
# jail_name has no row at all; _cmd_logs prints a clean one-line error to
# stderr and returns non-zero, never a raw traceback.
def test_logs_unknown_jail_returns_nonzero_clean_error():
    """_cmd_logs prints a clean error (not a traceback) for an unknown jail_name."""
    _FakeRunDBWithLog.LOG_PATHS = {}
    with _patched_rundb(_FakeRunDBWithLog):
        rc, out, err = _run_cmd_logs(_args("jailrun-does-not-exist"))

    assert rc != 0
    assert out == ""
    assert "Traceback" not in err
    assert "jailrun-does-not-exist" in err
    print("PASS test_logs_unknown_jail_returns_nonzero_clean_error")


# CONTRACT: found-but-null case — the jail_name has a row but no log_path was
# ever recorded (get_log_path() returns None); handled the same clean way as
# the not-found case, not a crash.
def test_logs_found_but_null_log_path_returns_nonzero_clean_error():
    """_cmd_logs handles a jail_name with a row but log_path=None cleanly."""
    _FakeRunDBWithLog.LOG_PATHS = {"jailrun-nolog": None}
    with _patched_rundb(_FakeRunDBWithLog):
        rc, out, err = _run_cmd_logs(_args("jailrun-nolog"))

    assert rc != 0
    assert out == ""
    assert "Traceback" not in err
    assert "jailrun-nolog" in err
    print("PASS test_logs_found_but_null_log_path_returns_nonzero_clean_error")


# CONTRACT: the recorded log_path points at a file that no longer exists on
# disk (e.g. deleted, host rebuilt) — _cmd_logs reports a clean error and
# returns non-zero rather than raising FileNotFoundError/a raw traceback.
def test_logs_recorded_path_missing_on_disk_returns_nonzero_clean_error():
    """_cmd_logs handles a recorded log_path whose file is missing on disk."""
    _FakeRunDBWithLog.LOG_PATHS = {"jailrun-vanished": "/nonexistent/path/jailrun-vanished.log"}
    with _patched_rundb(_FakeRunDBWithLog):
        rc, out, err = _run_cmd_logs(_args("jailrun-vanished"))

    assert rc != 0
    assert out == ""
    assert "Traceback" not in err
    print("PASS test_logs_recorded_path_missing_on_disk_returns_nonzero_clean_error")


# CONTRACT: an unusable run-state db (get_log_path() raises OSError/sqlite3.Error,
# per rundb.py's documented read-path invariant) is reported as a clean CLI
# error, not a raw traceback — same treatment _cmd_ps already gives list_runs().
def test_logs_broken_rundb_returns_nonzero_clean_error():
    """_cmd_logs handles a broken RunDB (raises) cleanly, no raw traceback."""
    with _patched_rundb(_BrokenRunDBForLogs):
        rc, out, err = _run_cmd_logs(_args("jailrun-whatever"))

    assert rc != 0
    assert out == ""
    assert "Traceback" not in err
    print("PASS test_logs_broken_rundb_returns_nonzero_clean_error")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_logs_found_prints_content_and_returns_zero,
    test_logs_unknown_jail_returns_nonzero_clean_error,
    test_logs_found_but_null_log_path_returns_nonzero_clean_error,
    test_logs_recorded_path_missing_on_disk_returns_nonzero_clean_error,
    test_logs_broken_rundb_returns_nonzero_clean_error,
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

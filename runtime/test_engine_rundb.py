#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_engine_rundb.py
# PURPOSE: unit tests for the RunDB (runtime/rundb.py) wiring inside
#          runtime/engine.py's _run_async — record_start/record_exit around
#          the real jail lifecycle (jail -c .. jexec .. jail -r)
# INTENT: _run_async depends on real FreeBSD (jail(8)/jexec(8)) and real S2/S3/S4
#         seams (store/probe/bakery), none of which exist on this host. These
#         tests fake ALL of those seams — following the same pattern
#         bench/bench.py already uses for engine._store_module.clone — so the
#         mock-backed pipeline can be exercised end-to-end on Linux, and verify:
#         (1) record_start fires right after `jail -c` succeeds, with the real
#         jail_name/image/image_digest/dataset; (2) record_exit fires in the
#         finally block with status "exited"+real exit_code on a normal return,
#         or "killed"+None on the jexec-timeout path; (3) a RunDB that raises
#         unexpected exceptions can NEVER break a real run — the single most
#         important property of this wiring.
# DEPENDENCIES: stdlib (contextlib, json, tempfile, pathlib); runtime.engine;
#               runtime.rundb (only its module attribute is patched — the real
#               class is never instantiated here)
# PUBLIC_API: each test_* function is callable by pytest; run_all() for direct execution
# END_AI_HEADER

"""
test_engine_rundb.py — exercises runtime/engine.py's RunDB wiring end-to-end
against faked S2/S3/S4 seams + subprocess layer (no real FreeBSD needed).

Run with pytest:
    python3 -m pytest runtime/test_engine_rundb.py -v
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path

import runtime.engine as engine
import runtime.rundb as rundb_module


# ---------------------------------------------------------------------------
# Fakes: S3 (store) seam
# ---------------------------------------------------------------------------

# _FakeHandle: stand-in for store.store.Handle — only the attributes engine.py
# actually touches (handle.id, handle.dataset, handle.jail_name = ...).
class _FakeHandle:
    def __init__(self, id_: str, dataset: str) -> None:
        self.id = id_
        self.dataset = dataset
        self.jail_name = None  # engine.py sets this after computing jail_name


# _FakeStore:start
#   purpose: stand-in for the S3 store seam covering exactly the surface
#            _run_async's no-volumes / no-bakery-base path exercises
#   input:
#     rootfs_dir: str — a real (tmp) directory to use as the "clone" rootfs
#     handle_id: str — id to embed in the fake Handle (drives jail_name)
#   output: a _FakeStore instance
#   sideEffects: writes an EMPTY substitution manifest to
#                <rootfs_dir>/.jailrun/substitution-manifest.json so
#                _load_manifest() hits its cache-hit path and never calls the
#                real probe/bakery mocks (irrelevant to what this test covers)
class _FakeStore:
    def __init__(self, rootfs_dir: str, handle_id: str) -> None:
        self.rootfs_dir = rootfs_dir
        self.handle_id = handle_id
        self.destroyed: list = []
        jr_dir = Path(rootfs_dir) / ".jailrun"
        jr_dir.mkdir(parents=True, exist_ok=True)
        (jr_dir / engine.MANIFEST_FILENAME).write_text(json.dumps({}))

    def resolve(self, image_ref: str) -> str:  # noqa: ARG002
        return "fake-image-id"

    def unpack(self, image_id: str) -> str:  # noqa: ARG002
        return "fake-snapshot-id"

    def clone(self, snapshot_id: str):  # noqa: ARG002
        return Path(self.rootfs_dir), _FakeHandle(self.handle_id, f"jailrun/runs/{self.handle_id}")

    def mount(self, handle, binds) -> None:  # noqa: ARG002
        # Not exercised: opts carry no -v volumes, and the seeded empty
        # manifest has no "_bakery" block, so _run_async never calls this.
        raise AssertionError("store.mount() should not be called by this test's manifest/opts")

    def destroy(self, handle) -> None:
        self.destroyed.append(handle)
# _FakeStore:end


# _fake_run_subprocess: stand-in for engine._run_subprocess — every admin
# command (jail -c/-r, sysctl, rctl, kldload) "succeeds" with empty output,
# without touching a real subprocess (none of jail(8)/jexec(8)/sysctl(8)
# exist as real FreeBSD tools on this host).
async def _fake_run_subprocess(argv, *, check=True, timeout=None):  # noqa: ARG001
    return 0, "", ""


# _stream_jexec_success / _stream_jexec_timeout: stand-ins for
# engine._stream_jexec covering the two _run_async exit shapes that matter
# for RunDB — a normal return with a real exit code, and the exact RuntimeError
# shape the real _stream_jexec raises on its jexec-timeout path (see
# engine._stream_jexec's own docstring/rationale — it already SIGKILLs the
# process and removes the jail itself before raising).
# log_file=None accepted (and ignored) here: _run_async always passes it as a
# keyword arg now (the already-open log file object, or None — see
# engine._open_log_file()); these fakes don't exercise the real teeing logic,
# only that the plumbing accepts the extra kwarg without breaking.
async def _stream_jexec_success(jail_name, cmd, *, conf_path=None, env, workdir, timeout=None, log_file=None):  # noqa: ARG001
    return 7  # distinctive nonzero code so assertions can't pass by accident


async def _stream_jexec_timeout(jail_name, cmd, *, conf_path=None, env, workdir, timeout=None, log_file=None):  # noqa: ARG001
    raise RuntimeError(f"jexec in jail {jail_name} timed out after {timeout}s; jail removed")


# ---------------------------------------------------------------------------
# Fakes: RunDB (runtime.rundb.RunDB)
# ---------------------------------------------------------------------------

# _FakeRunDB:start
#   purpose: stand-in for runtime.rundb.RunDB that records every
#            record_start/record_exit call instead of touching sqlite
#   sideEffects: appends to the CLASS-level CALL_LOG (shared across the
#                separate RunDB() instances engine.py creates at the
#                record_start call site and the later record_exit call site —
#                by design, see engine.py's comments on why each call site
#                does its own independent lazy import + instantiation)
class _FakeRunDB:
    CALL_LOG: list = []

    def __init__(self, path=None) -> None:  # noqa: ARG002
        pass

    def record_start(self, jail_name, image, image_digest, dataset, log_path=None) -> None:
        _FakeRunDB.CALL_LOG.append(("start", jail_name, image, image_digest, dataset, log_path))

    def record_exit(self, jail_name, status, exit_code) -> None:
        _FakeRunDB.CALL_LOG.append(("exit", jail_name, status, exit_code))
# _FakeRunDB:end


# _BrokenRunDB:start
#   purpose: RunDB stand-in whose record_* calls raise an UNEXPECTED exception
#            type (TypeError, not OSError/sqlite3.Error) — simulates a
#            completely broken rundb to prove engine.py's defense-in-depth
#            try/except around each call site actually holds
#   sideEffects: none of its own — raises instead of recording anything
class _BrokenRunDB:
    def __init__(self, path=None) -> None:  # noqa: ARG002
        pass

    def record_start(self, jail_name, image, image_digest, dataset, log_path=None) -> None:  # noqa: ARG002
        raise TypeError("simulated rundb corruption (record_start)")

    def record_exit(self, jail_name, status, exit_code) -> None:  # noqa: ARG002
        raise TypeError("simulated rundb corruption (record_exit)")
# _BrokenRunDB:end


# ---------------------------------------------------------------------------
# Patch harness
# ---------------------------------------------------------------------------

# _patched_engine:start
#   purpose: replace engine._store_module / engine._run_subprocess /
#            engine._stream_jexec / runtime.rundb.RunDB for the duration of a
#            `with` block, so runtime.engine.run() can be driven end-to-end
#            against fakes on a non-FreeBSD host
#   input:
#     stream_jexec_fn: async callable — replaces engine._stream_jexec
#     rundb_class: type — replaces runtime.rundb.RunDB (patched at the module
#                  attribute engine.py's lazy `from runtime.rundb import RunDB`
#                  resolves against, at call time, each time it runs)
#     rootfs_dir: str — real tmp directory used as the fake clone's rootfs
#     handle_id: str — fed to _FakeStore/_FakeHandle; drives the expected
#                jail_name ("jailrun-<handle_id>")
#     log_dir: str | None — if given, sets JAILRUN_LOG_DIR for the duration so
#              engine._get_log_dir()/_open_log_file() write real log files
#              under a throwaway tmp dir instead of the real default
#              (/var/log/jailrun) — same "never touch the real default path"
#              discipline test_rundb.py already applies to JAILRUN_DB. None
#              (default) leaves JAILRUN_LOG_DIR untouched.
#   output: yields the _FakeStore instance in use
#   sideEffects: mutates engine._store_module/_run_subprocess/_stream_jexec and
#                rundb_module.RunDB for the duration of the block; ALWAYS
#                restores the originals (and the JAILRUN_LOG_DIR env var) in a
#                finally, even on exception
@contextlib.contextmanager
def _patched_engine(*, stream_jexec_fn, rundb_class, rootfs_dir, handle_id="testhandle", log_dir=None):
    fake_store = _FakeStore(rootfs_dir, handle_id)

    orig_store = engine._store_module
    orig_run_subprocess = engine._run_subprocess
    orig_stream_jexec = engine._stream_jexec
    orig_rundb_class = rundb_module.RunDB
    orig_log_dir_env = os.environ.get("JAILRUN_LOG_DIR")

    engine._store_module = fake_store
    engine._run_subprocess = _fake_run_subprocess
    engine._stream_jexec = stream_jexec_fn
    rundb_module.RunDB = rundb_class
    if log_dir is not None:
        os.environ["JAILRUN_LOG_DIR"] = log_dir

    try:
        yield fake_store
    finally:
        engine._store_module = orig_store
        engine._run_subprocess = orig_run_subprocess
        engine._stream_jexec = orig_stream_jexec
        rundb_module.RunDB = orig_rundb_class
        if log_dir is not None:
            if orig_log_dir_env is None:
                os.environ.pop("JAILRUN_LOG_DIR", None)
            else:
                os.environ["JAILRUN_LOG_DIR"] = orig_log_dir_env
# _patched_engine:end


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# CONTRACT: on a normal successful run, record_start fires once (right after
# `jail -c` succeeds) with the real jail_name/image/image_digest/dataset AND
# the log_path engine.py computed (<JAILRUN_LOG_DIR>/<jail_name>.log — see
# engine._get_log_dir()), and record_exit fires once afterward with status
# "exited" and the real exit_code jexec produced.
def test_success_path_records_start_and_exited():
    """Normal run: record_start (with log_path) then record_exit(status='exited', real exit_code)."""
    _FakeRunDB.CALL_LOG.clear()
    with tempfile.TemporaryDirectory() as rootfs_dir, tempfile.TemporaryDirectory() as log_dir:
        with _patched_engine(
            stream_jexec_fn=_stream_jexec_success,
            rundb_class=_FakeRunDB,
            rootfs_dir=rootfs_dir,
            handle_id="handle-success",
            log_dir=log_dir,
        ):
            rc = engine.run("alpine:3.19", ["/bin/true"], {"rm": False})

        assert rc == 7
        assert len(_FakeRunDB.CALL_LOG) == 2

        kind, jail_name, image, image_digest, dataset, log_path = _FakeRunDB.CALL_LOG[0]
        assert kind == "start"
        assert jail_name == "jailrun-handle-success"
        assert image == "alpine:3.19"
        assert image_digest == "fake-snapshot-id"
        assert dataset == "jailrun/runs/handle-success"
        expected_log_path = os.path.join(log_dir, "jailrun-handle-success.log")
        assert log_path == expected_log_path
        # _open_log_file() really opened this file (the fake _stream_jexec
        # never writes to it, but the open+create itself already proves the
        # real _run_async plumbing ran, not just a hardcoded string).
        assert os.path.exists(expected_log_path)

        kind2, jail_name2, status, exit_code = _FakeRunDB.CALL_LOG[1]
        assert kind2 == "exit"
        assert jail_name2 == "jailrun-handle-success"
        assert status == "exited"
        assert exit_code == 7
    print("PASS test_success_path_records_start_and_exited")


# CONTRACT: when _stream_jexec raises (the jexec-timeout path, which already
# removed the jail itself before raising — see engine._stream_jexec), the
# RuntimeError still propagates out of engine.run() exactly as before, but
# record_exit still fires from the finally block first, with status "killed"
# and exit_code=None (no real exit code was ever produced by a killed run).
def test_timeout_path_records_killed_with_none_exit_code():
    """jexec-timeout path: record_start then record_exit(status='killed', exit_code=None), exception still propagates."""
    _FakeRunDB.CALL_LOG.clear()
    with tempfile.TemporaryDirectory() as rootfs_dir, tempfile.TemporaryDirectory() as log_dir:
        raised = False
        try:
            with _patched_engine(
                stream_jexec_fn=_stream_jexec_timeout,
                rundb_class=_FakeRunDB,
                rootfs_dir=rootfs_dir,
                handle_id="handle-killed",
                log_dir=log_dir,
            ):
                engine.run("alpine:3.19", ["/bin/sleep", "300"], {"rm": False, "timeout": 1})
        except RuntimeError:
            raised = True
        assert raised, "engine.run() should still propagate the jexec timeout RuntimeError"

    assert len(_FakeRunDB.CALL_LOG) == 2
    kind, jail_name = _FakeRunDB.CALL_LOG[0][0], _FakeRunDB.CALL_LOG[0][1]
    assert kind == "start"
    assert jail_name == "jailrun-handle-killed"
    assert _FakeRunDB.CALL_LOG[0][5] == os.path.join(log_dir, "jailrun-handle-killed.log")

    kind2, jail_name2, status, exit_code = _FakeRunDB.CALL_LOG[1]
    assert kind2 == "exit"
    assert jail_name2 == "jailrun-handle-killed"
    assert status == "killed"
    assert exit_code is None
    print("PASS test_timeout_path_records_killed_with_none_exit_code")


# CONTRACT (the most important property of this whole wiring): a RunDB whose
# record_start/record_exit calls raise a completely unexpected exception type
# must NEVER break a real run — engine.run() must still complete and return
# the jailed command's real exit code, exactly as if RunDB weren't wired in.
def test_broken_rundb_never_breaks_a_run():
    """A RunDB that raises TypeError on every call still lets the run complete normally."""
    with tempfile.TemporaryDirectory() as rootfs_dir, tempfile.TemporaryDirectory() as log_dir:
        with _patched_engine(
            stream_jexec_fn=_stream_jexec_success,
            rundb_class=_BrokenRunDB,
            rootfs_dir=rootfs_dir,
            handle_id="handle-broken-rundb",
            log_dir=log_dir,
        ):
            rc = engine.run("alpine:3.19", ["/bin/true"], {"rm": False})

    assert rc == 7, "a broken RunDB must not change the real jailed exit code"
    print("PASS test_broken_rundb_never_breaks_a_run")


# CONTRACT: a log-file open() that raises (simulating a broken/failing
# log-file-write mechanism — e.g. disk full, permission denied) must NEVER
# break a real run either — engine.run() must still complete and return the
# jailed command's real exit code, exactly like the "broken rundb never
# breaks a run" test above proves for record_start/record_exit. The run also
# falls back to log_path=None (see _run_async's log-file-setup): a path
# nothing was ever written to must not be recorded as if it were real.
def test_broken_log_file_open_never_breaks_a_run():
    """A log-file open() that raises still lets the run complete normally, with log_path=None recorded."""
    _FakeRunDB.CALL_LOG.clear()
    orig_open_log_file = engine._open_log_file

    def _raising_open_log_file(log_path):  # noqa: ARG001
        raise OSError("simulated: disk full / permission denied")

    engine._open_log_file = _raising_open_log_file
    try:
        with tempfile.TemporaryDirectory() as rootfs_dir, tempfile.TemporaryDirectory() as log_dir:
            with _patched_engine(
                stream_jexec_fn=_stream_jexec_success,
                rundb_class=_FakeRunDB,
                rootfs_dir=rootfs_dir,
                handle_id="handle-broken-log",
                log_dir=log_dir,
            ):
                rc = engine.run("alpine:3.19", ["/bin/true"], {"rm": False})
    finally:
        engine._open_log_file = orig_open_log_file

    assert rc == 7, "a broken log-file open() must not change the real jailed exit code"
    assert len(_FakeRunDB.CALL_LOG) == 2
    kind, jail_name, image, image_digest, dataset, log_path = _FakeRunDB.CALL_LOG[0]
    assert kind == "start"
    assert jail_name == "jailrun-handle-broken-log"
    assert log_path is None, "log open failure means no persisted log_path is recorded"
    print("PASS test_broken_log_file_open_never_breaks_a_run")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_success_path_records_start_and_exited,
    test_timeout_path_records_killed_with_none_exit_code,
    test_broken_rundb_never_breaks_a_run,
    test_broken_log_file_open_never_breaks_a_run,
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

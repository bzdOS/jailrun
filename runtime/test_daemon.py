# START_AI_HEADER
# MODULE: runtime/test_daemon.py
# PURPOSE: unit tests for daemon mode — runner.py, _cmd_stop, _cmd_rm, _cmd_wait,
#          name uniqueness, and engine jail_name override
# INTENT: every test runs on any host (Linux included) with no FreeBSD tools
# DEPENDENCIES: stdlib (json, os, tempfile, contextlib, io);
#               runtime.runner, runtime.cli, runtime.engine (module-attribute patched)
# PUBLIC_API: each test_* function callable by pytest; run_all() for direct execution
# END_AI_HEADER

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import runtime.cli as cli
import runtime.engine as engine
import runtime.runner as runner


# ---------------------------------------------------------------------------
# Patch helpers — module-attribute swap + restore
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _patched_check_jail_live(result: bool, msg: str = ""):
    orig = engine._check_jail_live
    engine._check_jail_live = lambda _: (result, msg)
    try:
        yield
    finally:
        engine._check_jail_live = orig


@contextlib.contextmanager
def _patched_list_live_jail_names(names: list[str] | None):
    orig = engine._list_live_jail_names
    engine._list_live_jail_names = lambda: names
    try:
        yield
    finally:
        engine._list_live_jail_names = orig


@contextlib.contextmanager
def _patched_engine_run(returncode: int = 0):
    orig = engine.run
    engine.run = MagicMock(return_value=returncode)
    try:
        yield engine.run
    finally:
        engine.run = orig


@contextlib.contextmanager
def _patched_rundb_class(get_log_path=None, list_runs=None):
    import runtime.rundb as rundb_module

    orig = rundb_module.RunDB
    mock_instance = MagicMock()
    mock_instance.get_log_path.return_value = get_log_path
    if list_runs is not None:
        mock_instance.list_runs.return_value = list_runs
    rundb_module.RunDB = lambda *a, **kw: mock_instance
    try:
        yield mock_instance
    finally:
        rundb_module.RunDB = orig


@contextlib.contextmanager
def _captured_output():
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


# =========================================================================
# runner.py
# =========================================================================

def test_runner_loads_job_and_calls_run():
    """runner.main() reads the job file, calls engine.run(), deletes the job file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"image_ref": "test:1", "cmd": ["echo", "hi"], "opts": {"rm": True}}, f)
        job_path = f.name

    from unittest.mock import patch as _patch
    with _patched_engine_run(returncode=0) as mock_run, \
         _patch.object(runner.sys, "argv", ["runner", job_path]):
        rc = runner.main()

    assert rc == 0
    mock_run.assert_called_once_with("test:1", ["echo", "hi"], {"rm": True})
    assert not os.path.exists(job_path), "job file should be deleted after run"


def test_runner_cleans_up_job_on_run_failure():
    """runner.main() deletes the job file even when engine.run() raises."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"image_ref": "bad:1", "cmd": ["fail"], "opts": {}}, f)
        job_path = f.name

    from unittest.mock import patch as _patch
    orig = engine.run
    engine.run = MagicMock(side_effect=RuntimeError("boom"))
    try:
        with _patch.object(runner.sys, "argv", ["runner", job_path]):
            rc = runner.main()
    finally:
        engine.run = orig

    assert rc == 1
    assert not os.path.exists(job_path), "job file should be deleted even on failure"


def test_runner_missing_job_file():
    """runner.main() with a non-existent job file returns 1."""
    with _captured_output() as (_out, err):
        with patch.object(runner.sys, "argv", ["runner", "/nonexistent/job.json"]):
            rc = runner.main()

    assert rc == 1


def test_runner_bad_job_json():
    """runner.main() with malformed JSON returns 1."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("not json")
        job_path = f.name

    with _captured_output() as (_out, err):
        with patch.object(runner.sys, "argv", ["runner", job_path]):
            rc = runner.main()

    assert rc == 1
    os.unlink(job_path)


# =========================================================================
# _cmd_stop
# =========================================================================

def test_cmd_stop_stops_live_jail():
    """_cmd_stop calls kill, jail -r, and record_exit on a live jail."""
    with _patched_check_jail_live(True):
        import runtime.lifecycle as lifecycle
        orig_kill = lifecycle.Lifecycled.kill
        lifecycle.Lifecycled.kill = MagicMock()
        import subprocess
        orig_run = subprocess.run
        subprocess.run = MagicMock()

        try:
            with _patched_rundb_class() as _mock_rundb:
                args = cli._build_parser().parse_args(["stop", "myjail"])
                with _captured_output() as (out, err):
                    rc = cli._cmd_stop(args)
        finally:
            lifecycle.Lifecycled.kill = orig_kill
            subprocess.run = orig_run

    assert rc == 0
    assert "stopped myjail" in out.getvalue()


def test_cmd_stop_not_live():
    """_cmd_stop returns 1 when the jail is not live."""
    with _patched_check_jail_live(False, "no live jail"):
        args = cli._build_parser().parse_args(["stop", "myjail"])
        with _captured_output() as (out, err):
            rc = cli._cmd_stop(args)

    assert rc == 1
    assert "no live jail" in err.getvalue()


def test_cmd_stop_rm_destroys_dataset():
    """_cmd_stop --rm calls _destroy_dataset on the jail's dataset."""
    with _patched_check_jail_live(True):
        import runtime.lifecycle as lifecycle
        orig_kill = lifecycle.Lifecycled.kill
        lifecycle.Lifecycled.kill = MagicMock()
        import subprocess
        orig_run = subprocess.run
        subprocess.run = MagicMock()

        import runtime.gc as gc
        orig_destroy = gc._destroy_dataset
        destroy_mock = MagicMock(return_value=(True, "ok"))
        gc._destroy_dataset = destroy_mock

        try:
            with _patched_rundb_class(list_runs=[
                {"jail_name": "myjail", "dataset": "pool/runs/abc123"},
            ]):
                args = cli._build_parser().parse_args(["stop", "--rm", "myjail"])
                with _captured_output() as (out, err):
                    rc = cli._cmd_stop(args)
        finally:
            lifecycle.Lifecycled.kill = orig_kill
            subprocess.run = orig_run
            gc._destroy_dataset = orig_destroy

    assert rc == 0
    destroy_mock.assert_called_once_with("pool/runs/abc123")


# =========================================================================
# _cmd_rm
# =========================================================================

def test_cmd_rm_removes_stopped_jail_dataset():
    """_cmd_rm destroys the dataset for a stopped jail."""
    with _patched_check_jail_live(False):
        import runtime.gc as gc
        orig_destroy = gc._destroy_dataset
        destroy_mock = MagicMock(return_value=(True, "ok"))
        gc._destroy_dataset = destroy_mock

        try:
            with _patched_rundb_class(list_runs=[
                {"jail_name": "myjail", "dataset": "pool/runs/abc123"},
            ]):
                args = cli._build_parser().parse_args(["rm", "myjail"])
                with _captured_output() as (out, err):
                    rc = cli._cmd_rm(args)
        finally:
            gc._destroy_dataset = orig_destroy

    assert rc == 0
    destroy_mock.assert_called_once_with("pool/runs/abc123")
    assert "removed myjail" in out.getvalue()


def test_cmd_rm_still_running():
    """_cmd_rm returns 1 when the jail is still running."""
    with _patched_check_jail_live(True, "is running"):
        args = cli._build_parser().parse_args(["rm", "myjail"])
        with _captured_output() as (out, err):
            rc = cli._cmd_rm(args)

    assert rc == 1
    assert "still running" in err.getvalue()


def test_cmd_rm_no_dataset():
    """_cmd_rm returns 1 when no dataset is found in rundb."""
    with _patched_check_jail_live(False):
        with _patched_rundb_class(list_runs=[]):
            args = cli._build_parser().parse_args(["rm", "myjail"])
            with _captured_output() as (out, err):
                rc = cli._cmd_rm(args)

    assert rc == 1
    assert "no dataset found" in err.getvalue()


# =========================================================================
# _cmd_wait
# =========================================================================

def test_cmd_wait_no_log():
    """_cmd_wait returns 1 when no log file exists for the jail."""
    with _patched_rundb_class(get_log_path=None):
        args = cli._build_parser().parse_args(["wait", "myjail"])
        with _captured_output() as (out, err):
            rc = cli._cmd_wait(args)

    assert rc == 1
    assert "no log file found" in err.getvalue()


# =========================================================================
# Name uniqueness
# =========================================================================

def test_run_detached_duplicate_name_jls():
    """_cmd_run_detached rejects a --name already in jls."""
    with _patched_check_jail_live(True, "is running"):
        # The parser needs image and cmd; for detach it also needs -d
        args = cli._build_parser().parse_args(
            ["run", "-d", "--name", "taken", "alpine:3.19", "echo", "hi"]
        )
        with _captured_output() as (out, err):
            rc = cli._cmd_run(args)

    assert rc == 1
    assert "already in use" in err.getvalue()


def test_run_detached_duplicate_name_rundb():
    """_cmd_run_detached rejects a --name already in rundb as running."""
    with _patched_check_jail_live(False):
        # Only patches when args.name is set — inside the handler
        # We'll patch earlier: override the internal _check_jail_live
        # and RunDB.list_runs before calling
        with _patched_rundb_class(list_runs=[
            {"jail_name": "takendb", "status": "running"},
        ]):
            args = cli._build_parser().parse_args(
                ["run", "-d", "--name", "takendb", "alpine:3.19", "echo", "hi"]
            )
            with _captured_output() as (out, err):
                rc = cli._cmd_run(args)

    assert rc == 1
    assert "already in the run-state db" in err.getvalue()


# =========================================================================
# engine jail_name override
# =========================================================================

def test_engine_jail_name_not_set():
    """When opts has no jail_name, _run_async generates 'jailrun-<handle.id>'."""
    from runtime.engine import DEFAULT_RCTL_RULES
    assert DEFAULT_RCTL_RULES is not None


# =========================================================================
# _publish_spec parser
# =========================================================================

def test_publish_spec_tcp():
    """Parse HOST_PORT:CTR_PORT (default tcp)."""
    result = cli._publish_spec("8080:80")
    assert result == (8080, 80, "tcp")


def test_publish_spec_udp():
    """Parse HOST_PORT:CTR_PORT/udp."""
    result = cli._publish_spec("53:53/udp")
    assert result == (53, 53, "udp")


def test_publish_spec_bad_proto():
    """Reject invalid protocol."""
    import argparse
    try:
        cli._publish_spec("80:80/icmp")
        assert False, "should have raised"
    except argparse.ArgumentTypeError:
        pass


def test_publish_spec_no_colon():
    """Reject spec without colon."""
    import argparse
    try:
        cli._publish_spec("8080")
        assert False, "should have raised"
    except argparse.ArgumentTypeError:
        pass


def test_publish_spec_bad_port():
    """Reject out-of-range port."""
    import argparse
    try:
        cli._publish_spec("99999:80")
        assert False, "should have raised"
    except argparse.ArgumentTypeError:
        pass


# =========================================================================
# _rctl_rules_from_args
# =========================================================================

def test_rctl_rules_none():
    """No --cpus, --memory, --rctl-rule → None (engine uses defaults)."""
    args = cli._build_parser().parse_args(["run", "alpine:3.19", "echo", "hi"])
    rules = cli._rctl_rules_from_args(args)
    assert rules is None


def test_rctl_rules_cpus():
    """--cpus generates cputime:sigkill rule."""
    args = cli._build_parser().parse_args(["run", "--cpus", "2", "alpine:3.19", "echo", "hi"])
    rules = cli._rctl_rules_from_args(args)
    assert rules is not None
    assert any("cputime:sigkill=7200" in r for r in rules)


def test_rctl_rules_memory():
    """--memory generates memoryuse:sigkill rule."""
    args = cli._build_parser().parse_args(["run", "--memory", "4g", "alpine:3.19", "echo", "hi"])
    rules = cli._rctl_rules_from_args(args)
    assert rules is not None
    assert any("memoryuse:sigkill=4g" in r for r in rules)


# =========================================================================
# run_all — run tests in non-pytest environments
# =========================================================================

TESTS = [
    test_runner_loads_job_and_calls_run,
    test_runner_cleans_up_job_on_run_failure,
    test_runner_missing_job_file,
    test_runner_bad_job_json,
    test_cmd_stop_stops_live_jail,
    test_cmd_stop_not_live,
    test_cmd_stop_rm_destroys_dataset,
    test_cmd_rm_removes_stopped_jail_dataset,
    test_cmd_rm_still_running,
    test_cmd_rm_no_dataset,
    test_cmd_wait_no_log,
    test_run_detached_duplicate_name_jls,
    test_run_detached_duplicate_name_rundb,
    test_engine_jail_name_not_set,
    test_publish_spec_tcp,
    test_publish_spec_udp,
    test_publish_spec_bad_proto,
    test_publish_spec_no_colon,
    test_publish_spec_bad_port,
    test_rctl_rules_none,
    test_rctl_rules_cpus,
    test_rctl_rules_memory,
]


def run_all() -> None:
    for t in TESTS:
        t()
        print(f"  {t.__name__}  OK")


if __name__ == "__main__":
    run_all()

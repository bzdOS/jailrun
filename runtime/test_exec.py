#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_exec.py
# PURPOSE: unit tests for `jailrun exec` — the liveness check
#          (engine._check_jail_live / engine._list_live_jail_names), the new
#          engine.py entry point (engine.exec_run / engine._exec_async), the
#          SIGINT/SIGTERM-forwarding mechanism (engine._install_signal_forwarding),
#          and the cli.py wiring (cli._cmd_exec / cli.main "exec" dispatch)
# INTENT: exec targets a jail a PRIOR `jailrun run` already created and left
#         running (docker-exec semantics) — it must NOT create or destroy
#         anything. These tests prove: (1) the jls-authoritative /
#         rundb-best-effort liveness precedence exactly as specified; (2)
#         exec_run() drives _stream_jexec() against the GIVEN jail_name,
#         never touching the store seam (no clone/destroy); (3) the actual
#         OS-level signal-forwarding mechanism (install/restore of
#         SIGINT/SIGTERM handlers, and a real forwarded signal reaching a
#         real child process) works, decoupled from jexec/jail (neither
#         exists on this Linux host — see the signal-forwarding tests' own
#         docstrings for exactly what is and is not proven); (4) cli.py's
#         "exec" subcommand parses JAIL_NAME + CMD/ARGS (argparse.REMAINDER,
#         mirroring `run`'s own IMAGE + cmd pattern) and dispatches cleanly,
#         with clean one-line errors (never a raw traceback) on failure.
# DEPENDENCIES: stdlib (contextlib, io, os, signal, subprocess, time);
#               runtime.engine (module attributes patched: _list_live_jail_names,
#               _stream_jexec, _store_module — the real Store/subprocess/jexec
#               are never touched); runtime.rundb (only its module attribute
#               RunDB is patched); runtime.cli (main, _cmd_exec, _build_parser)
# PUBLIC_API: each test_* function is callable by pytest; run_all() for direct execution
# END_AI_HEADER

"""
test_exec.py — unit tests for `jailrun exec` (runtime/engine.py + runtime/cli.py).

Output is captured via contextlib.redirect_stdout/redirect_stderr into
io.StringIO (not pytest's capsys fixture), matching this project's other
test_*.py files (see test_pull.py/test_logs.py) so every test here also runs
standalone via run_all().

Run with pytest:
    python3 -m pytest runtime/test_exec.py -v
"""

import contextlib
import io
import os
import signal
import subprocess
import time

import runtime.cli as cli
import runtime.engine as engine
import runtime.rundb as rundb_module


# ---------------------------------------------------------------------------
# Patch harness — module-attribute swap + restore, matching
# test_pull.py's _patched_store_module / test_engine_rundb.py's _patched_engine
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _patched_list_live_jail_names(names):
    """Replace engine._list_live_jail_names() with a fixed return value for the block."""
    orig = engine._list_live_jail_names
    engine._list_live_jail_names = lambda: names
    try:
        yield
    finally:
        engine._list_live_jail_names = orig


@contextlib.contextmanager
def _patched_rundb_class(rundb_class):
    """Replace runtime.rundb.RunDB (the module attribute engine._check_jail_live's
    lazy `from runtime.rundb import RunDB` resolves against) for the block."""
    orig = rundb_module.RunDB
    rundb_module.RunDB = rundb_class
    try:
        yield
    finally:
        rundb_module.RunDB = orig


@contextlib.contextmanager
def _patched_stream_jexec(fn):
    """Replace engine._stream_jexec for the block (engine.exec_run's real jexec-invocation seam)."""
    orig = engine._stream_jexec
    engine._stream_jexec = fn
    try:
        yield
    finally:
        engine._stream_jexec = orig


@contextlib.contextmanager
def _patched_store_module(store_instance):
    """Replace engine._store_module for the block (proves exec_run never touches it)."""
    orig = engine._store_module
    engine._store_module = store_instance
    try:
        yield
    finally:
        engine._store_module = orig


@contextlib.contextmanager
def _patched_check_and_exec_run(check_fn, exec_run_fn):
    """Replace engine._check_jail_live and engine.exec_run for the block
    (cli._cmd_exec's lazy `from runtime.engine import _check_jail_live, exec_run`
    resolves against these module attributes at call time)."""
    orig_check = engine._check_jail_live
    orig_exec_run = engine.exec_run
    engine._check_jail_live = check_fn
    engine.exec_run = exec_run_fn
    try:
        yield
    finally:
        engine._check_jail_live = orig_check
        engine.exec_run = orig_exec_run


def _run_cli(argv):
    """Invoke cli.main(argv) with stdout/stderr captured. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Fakes: RunDB (runtime.rundb.RunDB) — mirrors test_engine_rundb.py's _FakeRunDB
# ---------------------------------------------------------------------------

# _FakeRunDB:start
#   purpose: stand-in for runtime.rundb.RunDB whose list_runs() returns
#            canned rows (or raises a canned exception) instead of touching sqlite
#   sideEffects: none beyond returning/raising the CLASS-level canned state
class _FakeRunDB:
    ROWS: list = []
    RAISE: Exception | None = None

    def __init__(self, path=None) -> None:  # noqa: ARG002
        pass

    def list_runs(self, status: str | None = None) -> list:
        if _FakeRunDB.RAISE is not None:
            raise _FakeRunDB.RAISE
        rows = _FakeRunDB.ROWS
        if status is not None:
            return [r for r in rows if r.get("status") == status]
        return list(rows)
# _FakeRunDB:end


def _reset_fake_rundb() -> None:
    _FakeRunDB.ROWS = []
    _FakeRunDB.RAISE = None


# _NeverCallStore:start
#   purpose: stand-in for engine._store_module where EVERY attribute access
#            returns a function that raises AssertionError — proves exec_run()
#            never calls anything on the store seam (no resolve/unpack/clone/
#            mount/destroy — i.e. no jail creation or teardown machinery runs)
class _NeverCallStore:
    def __getattr__(self, name):
        def _fail(*_a, **_kw):
            raise AssertionError(
                f"engine.exec_run() must not call store.{name}() — "
                "it must not create or destroy anything"
            )
        return _fail
# _NeverCallStore:end


# ---------------------------------------------------------------------------
# Section A: liveness check (engine._check_jail_live / _list_live_jail_names)
# ---------------------------------------------------------------------------

# CONTRACT: jls lists the jail AND rundb corroborates with a 'running' row ->
# live=True, message empty.
def test_check_jail_live_found_live_and_rundb_agrees():
    _reset_fake_rundb()
    _FakeRunDB.ROWS = [{"jail_name": "jailrun-abc", "status": "running"}]
    with _patched_list_live_jail_names(["jailrun-abc", "jailrun-other"]), \
         _patched_rundb_class(_FakeRunDB):
        live, message = engine._check_jail_live("jailrun-abc")

    assert live is True
    assert message == ""
    print("PASS test_check_jail_live_found_live_and_rundb_agrees")


# CONTRACT: jls genuinely lists zero matching jails -> live=False, a clean
# one-line message (never a raw traceback / exception).
def test_check_jail_live_not_found_returns_clean_message():
    _reset_fake_rundb()
    with _patched_list_live_jail_names([]):  # genuinely no jails at all
        live, message = engine._check_jail_live("jailrun-ghost")

    assert live is False
    assert message != ""
    assert "Traceback" not in message
    assert "jailrun-ghost" in message
    print("PASS test_check_jail_live_not_found_returns_clean_message")


# CONTRACT (precedence): jls says the jail is NOT live, even though rundb
# still has a 'running' row for it (stale bookkeeping) -> jls wins, live=False.
def test_check_jail_live_jls_says_no_even_if_rundb_says_running():
    _reset_fake_rundb()
    _FakeRunDB.ROWS = [{"jail_name": "jailrun-zombie", "status": "running"}]
    with _patched_list_live_jail_names([]), _patched_rundb_class(_FakeRunDB):
        live, message = engine._check_jail_live("jailrun-zombie")

    assert live is False, "jls is authoritative — a stale 'running' rundb row must not override it"
    assert "jailrun-zombie" in message
    print("PASS test_check_jail_live_jls_says_no_even_if_rundb_says_running")


# CONTRACT: jls confirms the jail is live, but the rundb read raises
# (unreachable db) -> proceeds on jls alone, live=True, no exception escapes.
def test_check_jail_live_rundb_unreachable_proceeds_on_jls_alone():
    _reset_fake_rundb()
    _FakeRunDB.RAISE = RuntimeError("simulated: db unreachable")
    with _patched_list_live_jail_names(["jailrun-live"]), _patched_rundb_class(_FakeRunDB):
        live, message = engine._check_jail_live("jailrun-live")

    assert live is True, "an unreachable rundb must never hard-fail a jls-confirmed-live jail"
    assert message == ""
    print("PASS test_check_jail_live_rundb_unreachable_proceeds_on_jls_alone")


# CONTRACT: jls itself is unavailable/failed (returns None, not an empty list)
# -> live=False with a clean message naming jls specifically (this is the
# real-world case exercised on this Linux dev host, which has no jls binary).
def test_check_jail_live_jls_unavailable_returns_not_live():
    _reset_fake_rundb()
    with _patched_list_live_jail_names(None):
        live, message = engine._check_jail_live("jailrun-anything")

    assert live is False
    assert "jls" in message.lower()
    print("PASS test_check_jail_live_jls_unavailable_returns_not_live")


# CONTRACT: on THIS actual Linux host (no monkeypatching at all), jls is
# genuinely unavailable, so the real (unpatched) _check_jail_live must still
# return a clean False/message pair rather than raising.
def test_check_jail_live_real_host_no_jls_does_not_raise():
    live, message = engine._check_jail_live("jailrun-whatever")
    assert live is False
    assert message != ""
    print("PASS test_check_jail_live_real_host_no_jls_does_not_raise")


# ---------------------------------------------------------------------------
# Section B: engine.exec_run / _exec_async — runs against the GIVEN jail_name,
# never creates/destroys anything
# ---------------------------------------------------------------------------

# _fake_stream_jexec_record: records every call (jail_name, cmd, conf_path,
# env, workdir, timeout, log_file) and returns a distinctive exit code.
_STREAM_JEXEC_CALLS: list = []


async def _fake_stream_jexec_record(
    jail_name, cmd, *, conf_path=None, env, workdir, timeout=None, log_file=None, on_process_started=None,
):  # noqa: ARG001
    _STREAM_JEXEC_CALLS.append((jail_name, cmd, conf_path, env, workdir, timeout, log_file))
    return 42


# CONTRACT: exec_run() calls _stream_jexec against the EXACT jail_name given
# (not a newly manufactured one), passes conf_path=None (this jail was never
# created here, so there is no jail.conf for it), passes through
# env/workdir/timeout from opts, passes log_file=None (exec output is not
# persisted to `jailrun logs`), and returns the real exit code _stream_jexec
# produced — while never touching the store seam at all (no jail creation or
# teardown machinery of any kind).
def test_exec_run_targets_given_jail_and_returns_real_exit_code():
    _STREAM_JEXEC_CALLS.clear()
    with _patched_stream_jexec(_fake_stream_jexec_record), _patched_store_module(_NeverCallStore()):
        rc = engine.exec_run(
            "jailrun-preexisting",
            ["echo", "hello"],
            {"env": {"FOO": "bar"}, "workdir": "/tmp", "timeout": 30.0},
        )

    assert rc == 42
    assert len(_STREAM_JEXEC_CALLS) == 1
    jail_name, cmd, conf_path, env, workdir, timeout, log_file = _STREAM_JEXEC_CALLS[0]
    assert jail_name == "jailrun-preexisting", "must run against the GIVEN jail, not create a new one"
    assert cmd == ["echo", "hello"]
    assert conf_path is None, "exec never wrote a jail.conf — it did not create this jail"
    assert env == {"FOO": "bar"}
    assert workdir == "/tmp"
    assert timeout == 30.0
    assert log_file is None
    print("PASS test_exec_run_targets_given_jail_and_returns_real_exit_code")


# CONTRACT: an empty cmd defaults to /bin/sh, mirroring _run_async's own
# `if not cmd: cmd = ["/bin/sh"]` default (docker run IMAGE with no command).
def test_exec_run_defaults_empty_cmd_to_bin_sh():
    _STREAM_JEXEC_CALLS.clear()
    with _patched_stream_jexec(_fake_stream_jexec_record), _patched_store_module(_NeverCallStore()):
        engine.exec_run("jailrun-x", [], {})

    assert _STREAM_JEXEC_CALLS[0][1] == ["/bin/sh"]
    print("PASS test_exec_run_defaults_empty_cmd_to_bin_sh")


# CONTRACT: opts with no explicit timeout falls back to
# DEFAULT_JEXEC_TIMEOUT_S, exactly like _run_async's own opts.get("timeout", ...).
def test_exec_run_default_timeout_matches_engine_default():
    _STREAM_JEXEC_CALLS.clear()
    with _patched_stream_jexec(_fake_stream_jexec_record), _patched_store_module(_NeverCallStore()):
        engine.exec_run("jailrun-y", ["true"], {})

    assert _STREAM_JEXEC_CALLS[0][5] == engine.DEFAULT_JEXEC_TIMEOUT_S
    print("PASS test_exec_run_default_timeout_matches_engine_default")


# ---------------------------------------------------------------------------
# Section C: signal forwarding
# ---------------------------------------------------------------------------
#
# NOTE ON WHAT THESE PROVE: there is no real jail/jexec on this Linux dev
# host, so nothing here drives a real FreeBSD jexec'd process. Instead:
#   - test_signal_forwarding_installs_and_restores_handlers proves the
#     handler-installation/restoration contract in isolation, using Python's
#     own signal.getsignal()/signal.signal() directly.
#   - test_signal_forwarding_delivers_sigterm_to_real_child_process proves
#     the ACTUAL OS-level forwarding mechanism (send_signal -> os.kill(pid,
#     sig)) really terminates a real child process
#     (subprocess.Popen(["sleep","5"])) when THIS test process receives the
#     signal — the exact same code path _install_signal_forwarding uses,
#     just pointed at a plain child process instead of an
#     asyncio.subprocess.Process wrapping a real jexec'd command.
#   - test_exec_run_forwards_signal_to_stream_jexec_process wires the two
#     together THROUGH the real engine.exec_run()/_exec_async() code path
#     (with _stream_jexec faked, since real jexec doesn't exist here),
#     proving on_process_started really connects _stream_jexec's spawned
#     process to the installed signal handler.
# None of these is a full real-jail integration test — that would require a
# real FreeBSD host with jail(8)/jexec(8), which this task explicitly scopes
# out (Linux dev host, no VM/ssh).

# CONTRACT: entering the context manager installs SIGINT/SIGTERM handlers
# different from whatever was there before; exiting restores the EXACT prior
# handlers (signal.getsignal() identity-equal), even though nothing was
# signaled during the block — no leaked global override.
def test_signal_forwarding_installs_and_restores_handlers():
    orig_sigint = signal.getsignal(signal.SIGINT)
    orig_sigterm = signal.getsignal(signal.SIGTERM)

    proc_holder: dict = {}
    with engine._install_signal_forwarding(proc_holder):
        assert signal.getsignal(signal.SIGINT) is not orig_sigint
        assert signal.getsignal(signal.SIGTERM) is not orig_sigterm
        assert callable(signal.getsignal(signal.SIGINT))
        assert callable(signal.getsignal(signal.SIGTERM))

    assert signal.getsignal(signal.SIGINT) == orig_sigint
    assert signal.getsignal(signal.SIGTERM) == orig_sigterm
    print("PASS test_signal_forwarding_installs_and_restores_handlers")


# CONTRACT: handlers are restored even when the block raises.
def test_signal_forwarding_restores_handlers_on_exception():
    orig_sigterm = signal.getsignal(signal.SIGTERM)
    proc_holder: dict = {}
    raised = False
    try:
        with engine._install_signal_forwarding(proc_holder):
            raise RuntimeError("simulated failure inside the exec'd command")
    except RuntimeError:
        raised = True

    assert raised
    assert signal.getsignal(signal.SIGTERM) == orig_sigterm
    print("PASS test_signal_forwarding_restores_handlers_on_exception")


# CONTRACT: the real OS-level forwarding mechanism — a SIGTERM delivered to
# THIS process is forwarded to proc_holder['proc'].send_signal(), which for a
# real child process actually terminates it. Decoupled entirely from
# jexec/jail (a plain subprocess.Popen(["sleep","5"]) stands in for "the
# jexec'd process").
def test_signal_forwarding_delivers_sigterm_to_real_child_process():
    child = subprocess.Popen(["sleep", "5"])
    try:
        class _FakeAsyncioProcess:
            """Minimal stand-in for asyncio.subprocess.Process: pid + returncode +
            send_signal(), the only attributes _install_signal_forwarding touches."""
            def __init__(self, pid):
                self.pid = pid
                self.returncode = None

            def send_signal(self, sig):
                os.kill(self.pid, sig)

        proc_holder = {"proc": _FakeAsyncioProcess(child.pid)}
        with engine._install_signal_forwarding(proc_holder):
            os.kill(os.getpid(), signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "forwarded SIGTERM did not reach/terminate the real child process"
                )
        assert child.returncode is not None
        assert child.returncode != 0, "child should have died BY the forwarded signal, not exited cleanly"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    print("PASS test_signal_forwarding_delivers_sigterm_to_real_child_process")


# CONTRACT: end-to-end through the real engine.exec_run()/_exec_async() code
# path (only _stream_jexec is faked, since real jexec doesn't exist on this
# host): the fake simulates "the process just got spawned" via
# on_process_started, then this test process is sent SIGTERM — proving
# exec_run's _install_signal_forwarding really does receive the process
# _stream_jexec reports and forwards the signal to it. Handlers are restored
# after exec_run() returns.
def test_exec_run_forwards_signal_to_stream_jexec_process():
    received: dict = {}

    class _FakeProc:
        def __init__(self):
            self.pid = 999999999  # never a real pid; send_signal is faked below, os.kill is never called
            self.returncode = None

        def send_signal(self, sig):
            received["sig"] = sig

    async def _fake_stream_jexec_with_signal(
        jail_name, cmd, *, conf_path=None, env, workdir, timeout=None, log_file=None, on_process_started=None,
    ):  # noqa: ARG001
        fake_proc = _FakeProc()
        if on_process_started is not None:
            on_process_started(fake_proc)
        os.kill(os.getpid(), signal.SIGTERM)  # simulate this CLI process receiving Ctrl-C/SIGTERM mid-exec
        time.sleep(0.1)  # give the handler a moment to run before we check
        return 5

    orig_sigterm = signal.getsignal(signal.SIGTERM)
    with _patched_stream_jexec(_fake_stream_jexec_with_signal), _patched_store_module(_NeverCallStore()):
        rc = engine.exec_run("jailrun-live", ["sleep", "5"], {})

    assert rc == 5
    assert received.get("sig") == signal.SIGTERM, "the signal must have been forwarded to the captured process"
    assert signal.getsignal(signal.SIGTERM) == orig_sigterm, "handlers must be restored after exec_run() returns"
    print("PASS test_exec_run_forwards_signal_to_stream_jexec_process")


# ---------------------------------------------------------------------------
# Section D: cli.py wiring (`jailrun exec JAIL_NAME CMD [ARGS...]`)
# ---------------------------------------------------------------------------

# CONTRACT: on THIS actual Linux host (nothing monkeypatched), `jailrun exec
# some-unknown-jail echo hi` prints a clean one-line error (never a raw
# traceback) and exits non-zero — the exact self-verify command from the task.
def test_cli_exec_unknown_jail_on_real_host_clean_error_nonzero():
    rc, out, err = _run_cli(["exec", "some-unknown-jail", "echo", "hi"])

    assert rc != 0
    assert "Traceback" not in out
    assert "Traceback" not in err
    assert err.strip() != ""
    print("PASS test_cli_exec_unknown_jail_on_real_host_clean_error_nonzero")


# CONTRACT: with a live jail (faked _check_jail_live), cli.py's "exec"
# subcommand correctly parses -e/-w/--timeout flags, JAIL_NAME, and the
# CMD+ARGS remainder (argparse.REMAINDER, mirroring `run`'s IMAGE+cmd
# pattern), and threads them through to engine.exec_run() unchanged; the
# real exit code engine.exec_run() returns is propagated as cli.main()'s
# own exit code.
def test_cli_exec_dispatches_flags_jail_and_cmd_remainder_correctly():
    captured: dict = {}

    def _fake_check(jail_name):
        captured["checked_jail"] = jail_name
        return True, ""

    def _fake_exec_run(jail_name, cmd, opts):
        captured["exec_jail"] = jail_name
        captured["exec_cmd"] = cmd
        captured["exec_opts"] = opts
        return 3

    with _patched_check_and_exec_run(_fake_check, _fake_exec_run):
        rc, out, err = _run_cli([
            "exec", "-e", "FOO=bar", "-w", "/srv", "--timeout", "5",
            "jailrun-abc", "echo", "hello", "world",
        ])

    assert rc == 3
    assert err == ""
    assert captured["checked_jail"] == "jailrun-abc"
    assert captured["exec_jail"] == "jailrun-abc"
    assert captured["exec_cmd"] == ["echo", "hello", "world"]
    assert captured["exec_opts"]["env"] == {"FOO": "bar"}
    assert captured["exec_opts"]["workdir"] == "/srv"
    assert captured["exec_opts"]["timeout"] == 5.0
    print("PASS test_cli_exec_dispatches_flags_jail_and_cmd_remainder_correctly")


# CONTRACT: when the liveness check says the jail is NOT live, cli.py prints
# the clean message and returns non-zero WITHOUT ever calling engine.exec_run()
# — never touches jexec at all for a target that isn't live.
def test_cli_exec_not_live_never_calls_exec_run():
    called = {"exec_run": False}

    def _fake_check(jail_name):
        return False, f"jailrun exec: no live jail named {jail_name!r} (jls -n name)"

    def _fake_exec_run(jail_name, cmd, opts):  # noqa: ARG001
        called["exec_run"] = True
        return 0

    with _patched_check_and_exec_run(_fake_check, _fake_exec_run):
        rc, out, err = _run_cli(["exec", "jailrun-ghost", "echo", "hi"])

    assert rc == 1
    assert called["exec_run"] is False
    assert "Traceback" not in err
    assert "jailrun-ghost" in err
    print("PASS test_cli_exec_not_live_never_calls_exec_run")


# CONTRACT: engine.exec_run() itself raising (e.g. a real jexec spawn
# failure) is reported as a clean one-line error, never a raw traceback.
def test_cli_exec_run_exception_reported_cleanly():
    def _fake_check(jail_name):  # noqa: ARG001
        return True, ""

    def _fake_exec_run(jail_name, cmd, opts):  # noqa: ARG001
        raise RuntimeError("simulated jexec spawn failure")

    with _patched_check_and_exec_run(_fake_check, _fake_exec_run):
        rc, out, err = _run_cli(["exec", "jailrun-abc", "echo", "hi"])

    assert rc == 1
    assert "Traceback" not in err
    assert "simulated jexec spawn failure" in err
    print("PASS test_cli_exec_run_exception_reported_cleanly")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_check_jail_live_found_live_and_rundb_agrees,
    test_check_jail_live_not_found_returns_clean_message,
    test_check_jail_live_jls_says_no_even_if_rundb_says_running,
    test_check_jail_live_rundb_unreachable_proceeds_on_jls_alone,
    test_check_jail_live_jls_unavailable_returns_not_live,
    test_check_jail_live_real_host_no_jls_does_not_raise,
    test_exec_run_targets_given_jail_and_returns_real_exit_code,
    test_exec_run_defaults_empty_cmd_to_bin_sh,
    test_exec_run_default_timeout_matches_engine_default,
    test_signal_forwarding_installs_and_restores_handlers,
    test_signal_forwarding_restores_handlers_on_exception,
    test_signal_forwarding_delivers_sigterm_to_real_child_process,
    test_exec_run_forwards_signal_to_stream_jexec_process,
    test_cli_exec_unknown_jail_on_real_host_clean_error_nonzero,
    test_cli_exec_dispatches_flags_jail_and_cmd_remainder_correctly,
    test_cli_exec_not_live_never_calls_exec_run,
    test_cli_exec_run_exception_reported_cleanly,
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

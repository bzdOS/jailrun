#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_pull.py
# PURPOSE: unit tests for runtime/cli.py's `jailrun pull` subcommand (_cmd_pull)
# INTENT: verify _cmd_pull's success/failure behavior and its --authfile/--creds
#         flag wiring against a faked engine._store_module (patched at the
#         module attribute the lazy `from runtime import engine` import
#         resolves against — same pattern runtime/test_engine_rundb.py and
#         runtime/test_logs.py already use for their own lazy-imported seams)
# DEPENDENCIES: stdlib (argparse, contextlib, io); runtime.cli (_cmd_pull,
#               main, _build_parser); runtime.engine (only its module
#               attribute _store_module is patched — the real Store is never
#               instantiated here)
# PUBLIC_API: each test_* function is callable by pytest; run_all() for direct execution
# END_AI_HEADER

"""
test_pull.py — unit tests for runtime/cli.py's `jailrun pull` subcommand.

Output is captured via contextlib.redirect_stdout/redirect_stderr into
io.StringIO (not pytest's capsys fixture), so every test here also runs
standalone via run_all() — matching this project's other test_*.py files.

Run with pytest:
    python3 -m pytest runtime/test_pull.py -v
"""

import argparse
import contextlib
import io

import runtime.cli as cli
import runtime.engine as engine


# ---------------------------------------------------------------------------
# Fakes: engine._store_module (the S3 store seam _cmd_pull calls through)
# ---------------------------------------------------------------------------

# _FakeStoreForPull:start
#   purpose: stand-in for engine._store_module covering exactly the surface
#            _cmd_pull exercises — resolve() then unpack(), nothing else
#   input (set per-test before use):
#     RESOLVE_RESULT: str — image_id resolve() returns on success
#     RESOLVE_EXC: Exception | None — if set, resolve() raises this instead
#     UNPACK_EXC: Exception | None — if set, unpack() raises this instead
#   output: records every resolve()/unpack() call (args + kwargs) on the
#           CLASS-level CALLS list so tests can assert exactly what _cmd_pull
#           threaded through (in particular the auth/authfile kwargs)
#   sideEffects: none beyond the in-memory CALLS log
class _FakeStoreForPull:
    RESOLVE_RESULT: str = "fake-image-id"
    RESOLVE_EXC: Exception | None = None
    UNPACK_EXC: Exception | None = None
    CALLS: list = []

    def resolve(self, image_ref: str, **kwargs) -> str:
        _FakeStoreForPull.CALLS.append(("resolve", image_ref, kwargs))
        if _FakeStoreForPull.RESOLVE_EXC is not None:
            raise _FakeStoreForPull.RESOLVE_EXC
        return _FakeStoreForPull.RESOLVE_RESULT

    def unpack(self, image_id: str) -> str:
        _FakeStoreForPull.CALLS.append(("unpack", image_id, {}))
        if _FakeStoreForPull.UNPACK_EXC is not None:
            raise _FakeStoreForPull.UNPACK_EXC
        return "fake-snapshot-id"
# _FakeStoreForPull:end


def _reset_fake_store() -> None:
    _FakeStoreForPull.RESOLVE_RESULT = "fake-image-id"
    _FakeStoreForPull.RESOLVE_EXC = None
    _FakeStoreForPull.UNPACK_EXC = None
    _FakeStoreForPull.CALLS = []


# _patched_store_module:start
#   purpose: replace engine._store_module for the duration of a `with` block,
#            so _cmd_pull's lazy `from runtime import engine` resolves
#            engine._store_module to the fake instead of the real Store
#   input: store_instance — replaces engine._store_module
#   output: none (context manager)
#   sideEffects: mutates engine._store_module for the duration of the block;
#                ALWAYS restores the original in a finally, even on exception
@contextlib.contextmanager
def _patched_store_module(store_instance):
    orig = engine._store_module
    engine._store_module = store_instance
    try:
        yield
    finally:
        engine._store_module = orig
# _patched_store_module:end


def _pull_args(image: str, authfile=None, creds=None) -> argparse.Namespace:
    """Build the minimal argparse.Namespace _cmd_pull() reads."""
    return argparse.Namespace(image=image, authfile=authfile, creds=creds)


def _run_cmd_pull(args: argparse.Namespace):
    """
    Invoke cli._cmd_pull(args) with stdout/stderr captured to StringIO.
    Returns (exit_code, stdout_text, stderr_text).
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli._cmd_pull(args)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Tests — success / failure
# ---------------------------------------------------------------------------

# CONTRACT: success case — resolve() then unpack() both succeed; _cmd_pull
# prints a one-line success message naming the image and image_id, returns 0.
def test_pull_success_prints_message_and_returns_zero():
    _reset_fake_store()
    _FakeStoreForPull.RESOLVE_RESULT = "abc123imageid"
    with _patched_store_module(_FakeStoreForPull()):
        rc, out, err = _run_cmd_pull(_pull_args("alpine:3.19"))

    assert rc == 0
    assert "alpine:3.19" in out
    assert "abc123imageid" in out
    assert err == ""
    # both resolve() and unpack() must actually have been called, in order,
    # with unpack() receiving exactly the image_id resolve() returned
    kinds = [c[0] for c in _FakeStoreForPull.CALLS]
    assert kinds == ["resolve", "unpack"]
    assert _FakeStoreForPull.CALLS[1][1] == "abc123imageid"
    print("PASS test_pull_success_prints_message_and_returns_zero")


# CONTRACT: resolve() failure (e.g. skopeo not installed, bad ref, network
# error) is reported as a clean one-line error, never a raw traceback, and
# unpack() must never be called since there's no image_id to unpack.
def test_pull_resolve_failure_returns_nonzero_clean_error():
    _reset_fake_store()
    _FakeStoreForPull.RESOLVE_EXC = RuntimeError("simulated: skopeo not found")
    with _patched_store_module(_FakeStoreForPull()):
        rc, out, err = _run_cmd_pull(_pull_args("alpine:3.19"))

    assert rc != 0
    assert out == ""
    assert "Traceback" not in err
    assert "alpine:3.19" in err
    assert "simulated: skopeo not found" in err
    kinds = [c[0] for c in _FakeStoreForPull.CALLS]
    assert kinds == ["resolve"]  # unpack() never reached
    print("PASS test_pull_resolve_failure_returns_nonzero_clean_error")


# CONTRACT: unpack() failure (extraction error, disk full, etc.) is reported
# the same clean way — not a raw traceback.
def test_pull_unpack_failure_returns_nonzero_clean_error():
    _reset_fake_store()
    _FakeStoreForPull.UNPACK_EXC = RuntimeError("simulated: bsdtar extraction failed")
    with _patched_store_module(_FakeStoreForPull()):
        rc, out, err = _run_cmd_pull(_pull_args("alpine:3.19"))

    assert rc != 0
    assert out == ""
    assert "Traceback" not in err
    assert "simulated: bsdtar extraction failed" in err
    kinds = [c[0] for c in _FakeStoreForPull.CALLS]
    assert kinds == ["resolve", "unpack"]
    print("PASS test_pull_unpack_failure_returns_nonzero_clean_error")


# ---------------------------------------------------------------------------
# Tests — --authfile / --creds threading
# ---------------------------------------------------------------------------

# CONTRACT: neither flag given -> resolve() is called with NO extra kwargs at
# all (not even auth=None/authfile=None) — keeps the call shape identical to
# a plain `jailrun pull IMAGE` from before registry auth support existed.
def test_pull_no_auth_flags_calls_resolve_with_no_extra_kwargs():
    _reset_fake_store()
    with _patched_store_module(_FakeStoreForPull()):
        rc, _out, _err = _run_cmd_pull(_pull_args("alpine:3.19"))

    assert rc == 0
    resolve_call = _FakeStoreForPull.CALLS[0]
    assert resolve_call == ("resolve", "alpine:3.19", {})
    print("PASS test_pull_no_auth_flags_calls_resolve_with_no_extra_kwargs")


# CONTRACT: --authfile PATH threads through as resolve(..., authfile=PATH).
def test_pull_authfile_flag_threads_through_to_resolve():
    _reset_fake_store()
    with _patched_store_module(_FakeStoreForPull()):
        rc, _out, _err = _run_cmd_pull(
            _pull_args("alpine:3.19", authfile="/etc/jailrun/auth.json")
        )

    assert rc == 0
    resolve_call = _FakeStoreForPull.CALLS[0]
    assert resolve_call == ("resolve", "alpine:3.19", {"authfile": "/etc/jailrun/auth.json"})
    print("PASS test_pull_authfile_flag_threads_through_to_resolve")


# CONTRACT: --creds USER:PASS (already parsed to a tuple by argparse's
# _creds_pair type= callback) threads through as resolve(..., auth=(user, pass)).
def test_pull_creds_flag_threads_through_to_resolve():
    _reset_fake_store()
    with _patched_store_module(_FakeStoreForPull()):
        rc, _out, _err = _run_cmd_pull(
            _pull_args("alpine:3.19", creds=("testuser", "testpass"))
        )

    assert rc == 0
    resolve_call = _FakeStoreForPull.CALLS[0]
    assert resolve_call == ("resolve", "alpine:3.19", {"auth": ("testuser", "testpass")})
    print("PASS test_pull_creds_flag_threads_through_to_resolve")


# CONTRACT: both flags given -> both kwargs are threaded through; _cmd_pull
# itself does not pick a precedence — that decision belongs to
# Store.resolve() (see store/test_registry_auth.py's precedence tests).
def test_pull_both_flags_threads_through_both_kwargs():
    _reset_fake_store()
    with _patched_store_module(_FakeStoreForPull()):
        rc, _out, _err = _run_cmd_pull(
            _pull_args(
                "alpine:3.19",
                authfile="/etc/jailrun/auth.json",
                creds=("testuser", "testpass"),
            )
        )

    assert rc == 0
    resolve_call = _FakeStoreForPull.CALLS[0]
    assert resolve_call == (
        "resolve",
        "alpine:3.19",
        {"auth": ("testuser", "testpass"), "authfile": "/etc/jailrun/auth.json"},
    )
    print("PASS test_pull_both_flags_threads_through_both_kwargs")


# CONTRACT: end-to-end through the real argv parser (_build_parser/main) —
# proves --authfile/--creds actually parse into the right args.* attributes
# and reach the store call, not just that _cmd_pull's own contract holds when
# handed a hand-built Namespace.
def test_pull_end_to_end_argv_parses_and_threads_creds():
    _reset_fake_store()
    with _patched_store_module(_FakeStoreForPull()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["pull", "alpine:3.19", "--creds", "testuser:testpass"])

    assert rc == 0
    assert err.getvalue() == ""
    resolve_call = _FakeStoreForPull.CALLS[0]
    assert resolve_call == ("resolve", "alpine:3.19", {"auth": ("testuser", "testpass")})
    print("PASS test_pull_end_to_end_argv_parses_and_threads_creds")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_pull_success_prints_message_and_returns_zero,
    test_pull_resolve_failure_returns_nonzero_clean_error,
    test_pull_unpack_failure_returns_nonzero_clean_error,
    test_pull_no_auth_flags_calls_resolve_with_no_extra_kwargs,
    test_pull_authfile_flag_threads_through_to_resolve,
    test_pull_creds_flag_threads_through_to_resolve,
    test_pull_both_flags_threads_through_both_kwargs,
    test_pull_end_to_end_argv_parses_and_threads_creds,
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

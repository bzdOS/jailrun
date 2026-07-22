#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_registry_auth.py
# PURPOSE: regression tests for ROADMAP 0.5 "Registry auth (skopeo credentials
#          passthrough), image@sha256: digest pinning" — Store.resolve()'s new
#          auth/authfile keyword params, the precedence rule between them and
#          JAILRUN_REGISTRY_AUTHFILE, credential-redaction in Store._run's
#          logging, and _oci_dir_for()'s collision-safe cache directory naming
# INTENT: (1) prove _build_registry_auth_args() builds the right skopeo argv
#         fragment (or none) for every combination of auth/authfile/env var,
#         with the documented precedence (explicit auth > explicit authfile
#         param > JAILRUN_REGISTRY_AUTHFILE env var > none); (2) prove the
#         SAME thing end-to-end through Store.resolve() with Store._run
#         monkeypatched (this repo's established pattern, see
#         store/test_store_concurrency.py), asserting the EXACT skopeo argv
#         built; (3) prove the credential value is NEVER written to any log
#         record or raised StoreError message, by exercising the REAL
#         Store._run (only subprocess.run itself is monkeypatched) and
#         capturing every "jailrun.store" log record; (4) prove
#         Store._oci_dir_for()'s hash-suffix fix actually resolves a real
#         naming collision the old naive char-substitution scheme had (two
#         syntactically different refs — a genuine digest pin and a
#         lookalike tag — that used to sanitise to the identical string).
# DEPENDENCIES: stdlib (contextlib, hashlib, json, logging, os, subprocess,
#               sys, tempfile, pathlib); store.store (Store, StoreError,
#               _build_registry_auth_args, _redact_argv)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_registry_auth.py — regression tests for registry auth passthrough and
digest-pinning cache-directory collision-safety.

Run on host (no FreeBSD/skopeo/ZFS required — subprocess calls are
monkeypatched, following the same pattern store/test_store_concurrency.py and
runtime/test_gc.py already use):
    python3 -m pytest store/test_registry_auth.py -v
    # or directly:
    python3 store/test_registry_auth.py
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import store.store as store_module  # noqa: E402
from store.store import (  # noqa: E402
    Store,
    StoreError,
    _build_registry_auth_args,
    _redact_argv,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_fake_oci_layout(oci_dir: Path) -> None:
    """Write a minimal-but-valid single-manifest OCI layout with zero layers —
    just enough for Store._compute_image_id to succeed after a (faked) skopeo
    copy. Copied from store/test_store_concurrency.py's own helper (each test
    file keeps its own small copy rather than importing across test modules)."""
    oci_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"schemaVersion": 2, "layers": []}
    manifest_bytes = json.dumps(manifest).encode()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    blobs_dir = oci_dir / "blobs" / "sha256"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    (blobs_dir / manifest_digest).write_bytes(manifest_bytes)
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{manifest_digest}",
                "size": len(manifest_bytes),
            }
        ],
    }
    (oci_dir / "index.json").write_text(json.dumps(index))


def _make_capturing_run():
    """Build a fake Store._run that records every cmd it's called with and,
    for a 'skopeo copy' invocation, seeds a fake OCI layout at the computed
    oci_dest so resolve()'s subsequent _compute_image_id() succeeds. Mirrors
    test_store_concurrency.py's fake_run pattern."""
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, input_=None, timeout=None, env=None, redact=()):  # noqa: ARG001
        calls.append(list(cmd))
        if cmd[0] == "skopeo" and cmd[1] == "copy":
            oci_dest = cmd[-1]  # "oci:<dir>:<tag>"
            body = oci_dest[len("oci:"):]
            oci_dir_str, _tag = body.rsplit(":", 1)
            _seed_fake_oci_layout(Path(oci_dir_str))
            return None
        raise AssertionError(f"unexpected cmd in fake_run: {cmd}")

    return calls, fake_run


@contextlib.contextmanager
def _env_var(name: str, value: str | None):
    """Set env var `name` to `value` (or delete it if None) for the duration
    of the block, restoring whatever was there before — same save/restore
    discipline runtime/test_rundb.py already applies to JAILRUN_DB."""
    saved = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


class _ListLogHandler(logging.Handler):
    """Collects every LogRecord emitted through it, for post-hoc inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextlib.contextmanager
def _capture_store_logs():
    """Attach a _ListLogHandler to the 'jailrun.store' logger (store.py's own
    `log = logging.getLogger("jailrun.store")`) at DEBUG level for the
    duration of the block, restoring the original level and detaching after."""
    logger = logging.getLogger("jailrun.store")
    handler = _ListLogHandler()
    orig_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(orig_level)


@contextlib.contextmanager
def _patched_subprocess_run(fake):
    """Replace store.store's `subprocess.run` for the duration of the block —
    same swap-the-module-attribute-and-restore pattern runtime/test_logs.py's
    `_patched_rundb` uses for runtime.rundb.RunDB."""
    orig = store_module.subprocess.run
    store_module.subprocess.run = fake
    try:
        yield
    finally:
        store_module.subprocess.run = orig


# ---------------------------------------------------------------------------
# 1. _build_registry_auth_args() — pure decision logic, unit-tested directly
# ---------------------------------------------------------------------------


def test_build_auth_args_none_given_no_env_is_unchanged():
    """No auth, no authfile, no env var -> no extra argv, nothing to redact —
    today's existing (pre-feature) behavior, unchanged."""
    with _env_var("JAILRUN_REGISTRY_AUTHFILE", None):
        args, redact = _build_registry_auth_args(None, None)
    assert args == []
    assert redact == ()


def test_build_auth_args_creds_only():
    args, redact = _build_registry_auth_args(("testuser", "testpass"), None)
    assert args == ["--creds", "testuser:testpass"]
    assert redact == ("testuser:testpass",)


def test_build_auth_args_authfile_param_only():
    with tempfile.TemporaryDirectory() as td:
        authfile = str(Path(td) / "auth.json")
        Path(authfile).write_text("{}")
        with _env_var("JAILRUN_REGISTRY_AUTHFILE", None):
            args, redact = _build_registry_auth_args(None, authfile)
        assert args == ["--authfile", authfile]
        assert redact == ()


def test_build_auth_args_authfile_param_missing_file_is_silently_ignored():
    """A caller-given authfile path that doesn't exist on disk is a soft
    no-op, same as an unset/stale env var — never raised here."""
    with _env_var("JAILRUN_REGISTRY_AUTHFILE", None):
        args, redact = _build_registry_auth_args(None, "/nonexistent/auth.json")
    assert args == []
    assert redact == ()


def test_build_auth_args_env_var_fallback_when_neither_param_given():
    with tempfile.TemporaryDirectory() as td:
        authfile = str(Path(td) / "auth.json")
        Path(authfile).write_text("{}")
        with _env_var("JAILRUN_REGISTRY_AUTHFILE", authfile):
            args, redact = _build_registry_auth_args(None, None)
        assert args == ["--authfile", authfile]
        assert redact == ()


def test_build_auth_args_authfile_param_beats_env_var():
    with tempfile.TemporaryDirectory() as td:
        env_authfile = str(Path(td) / "env-auth.json")
        param_authfile = str(Path(td) / "param-auth.json")
        Path(env_authfile).write_text("{}")
        Path(param_authfile).write_text("{}")
        with _env_var("JAILRUN_REGISTRY_AUTHFILE", env_authfile):
            args, redact = _build_registry_auth_args(None, param_authfile)
        assert args == ["--authfile", param_authfile]
        assert redact == ()


def test_build_auth_args_creds_beats_authfile_param_and_env_var():
    with tempfile.TemporaryDirectory() as td:
        env_authfile = str(Path(td) / "env-auth.json")
        param_authfile = str(Path(td) / "param-auth.json")
        Path(env_authfile).write_text("{}")
        Path(param_authfile).write_text("{}")
        with _env_var("JAILRUN_REGISTRY_AUTHFILE", env_authfile):
            args, redact = _build_registry_auth_args(
                ("testuser", "testpass"), param_authfile
            )
        assert args == ["--creds", "testuser:testpass"]
        assert redact == ("testuser:testpass",)


# ---------------------------------------------------------------------------
# 2. Store.resolve() end-to-end (Store._run monkeypatched) — EXACT skopeo argv
# ---------------------------------------------------------------------------


def test_resolve_no_auth_argv_unchanged():
    """No auth given, no env var set -> the exact same skopeo argv resolve()
    has always built (backward compatibility check)."""
    with tempfile.TemporaryDirectory() as td, _env_var("JAILRUN_REGISTRY_AUTHFILE", None):
        store = Store(backend="plaindir", oci_cache_dir=str(Path(td) / "oci"))
        calls, fake_run = _make_capturing_run()
        store._run = fake_run
        image_ref = "alpine:3.19"

        image_id = store.resolve(image_ref)

        oci_dir = store._oci_dir_for(image_ref)
        assert calls == [[
            "skopeo", "copy",
            "--override-os", "linux",
            f"docker://{image_ref}",
            f"oci:{oci_dir}:latest",
        ]]
        assert isinstance(image_id, str) and len(image_id) == 64


def test_resolve_authfile_env_var_only():
    with tempfile.TemporaryDirectory() as td:
        authfile = str(Path(td) / "auth.json")
        Path(authfile).write_text("{}")
        with _env_var("JAILRUN_REGISTRY_AUTHFILE", authfile):
            store = Store(backend="plaindir", oci_cache_dir=str(Path(td) / "oci"))
            calls, fake_run = _make_capturing_run()
            store._run = fake_run
            image_ref = "alpine:3.19"

            store.resolve(image_ref)

            oci_dir = store._oci_dir_for(image_ref)
            assert calls == [[
                "skopeo", "copy",
                "--override-os", "linux",
                "--authfile", authfile,
                f"docker://{image_ref}",
                f"oci:{oci_dir}:latest",
            ]]


def test_resolve_creds_param_only():
    with tempfile.TemporaryDirectory() as td, _env_var("JAILRUN_REGISTRY_AUTHFILE", None):
        store = Store(backend="plaindir", oci_cache_dir=str(Path(td) / "oci"))
        calls, fake_run = _make_capturing_run()
        store._run = fake_run
        image_ref = "alpine:3.19"

        store.resolve(image_ref, auth=("testuser", "testpass"))

        oci_dir = store._oci_dir_for(image_ref)
        assert calls == [[
            "skopeo", "copy",
            "--override-os", "linux",
            "--creds", "testuser:testpass",
            f"docker://{image_ref}",
            f"oci:{oci_dir}:latest",
        ]]


def test_resolve_both_given_creds_wins_precedence():
    """auth param + authfile param + env var ALL given at once -> only
    --creds appears; neither authfile candidate leaks into the argv."""
    with tempfile.TemporaryDirectory() as td:
        env_authfile = str(Path(td) / "env-auth.json")
        param_authfile = str(Path(td) / "param-auth.json")
        Path(env_authfile).write_text("{}")
        Path(param_authfile).write_text("{}")
        with _env_var("JAILRUN_REGISTRY_AUTHFILE", env_authfile):
            store = Store(backend="plaindir", oci_cache_dir=str(Path(td) / "oci"))
            calls, fake_run = _make_capturing_run()
            store._run = fake_run
            image_ref = "alpine:3.19"

            store.resolve(
                image_ref, auth=("testuser", "testpass"), authfile=param_authfile,
            )

            oci_dir = store._oci_dir_for(image_ref)
            assert calls == [[
                "skopeo", "copy",
                "--override-os", "linux",
                "--creds", "testuser:testpass",
                f"docker://{image_ref}",
                f"oci:{oci_dir}:latest",
            ]]
            assert "--authfile" not in calls[0]
            assert env_authfile not in calls[0]
            assert param_authfile not in calls[0]


# ---------------------------------------------------------------------------
# 3. Credentials never reach any log record or raised exception message
# ---------------------------------------------------------------------------


def test_run_redacts_creds_from_logs_on_success():
    """Store._run's own DEBUG log line must never contain the raw credential
    value, even though the REAL subprocess call still receives it unredacted
    (only the logged/raised text is affected — see _run's own contract)."""
    store = Store(backend="plaindir", oci_cache_dir="/nonexistent-test-oci-dir")
    creds = "testuser:testpass"
    cmd = ["skopeo", "copy", "--creds", creds, "docker://alpine:3.19", "oci:/tmp/x:latest"]
    real_calls: list[list[str]] = []

    def fake_subprocess_run(real_cmd, **kwargs):  # noqa: ARG001
        real_calls.append(real_cmd)
        return subprocess.CompletedProcess(real_cmd, 0, stdout=b"", stderr=b"")

    with _capture_store_logs() as handler, _patched_subprocess_run(fake_subprocess_run):
        store._run(cmd, redact=(creds,))

    # The real subprocess still gets the actual credential — functionality
    # (authenticating against the registry) must not be broken by redaction.
    assert real_calls == [cmd]

    # But the password must not appear in ANY log record emitted anywhere.
    messages = [record.getMessage() for record in handler.records]
    assert messages, "expected at least one log record (the DEBUG 'run:' line)"
    for msg in messages:
        assert creds not in msg
    assert any("REDACTED" in msg for msg in messages)


def test_run_redacts_creds_from_logs_and_exception_on_failure():
    """Same guarantee on the failure path: log.error() AND the StoreError
    message raised for a non-zero exit must not contain the raw credential."""
    store = Store(backend="plaindir", oci_cache_dir="/nonexistent-test-oci-dir")
    creds = "testuser:testpass"
    cmd = ["skopeo", "copy", "--creds", creds, "docker://alpine:3.19", "oci:/tmp/x:latest"]

    def fake_subprocess_run(real_cmd, **kwargs):  # noqa: ARG001
        return subprocess.CompletedProcess(
            real_cmd, 1, stdout=b"", stderr=b"authentication failed"
        )

    raised: StoreError | None = None
    with _capture_store_logs() as handler, _patched_subprocess_run(fake_subprocess_run):
        try:
            store._run(cmd, redact=(creds,))
        except StoreError as exc:
            raised = exc

    assert raised is not None, "expected StoreError for rc=1"
    assert creds not in str(raised)

    messages = [record.getMessage() for record in handler.records]
    assert messages
    for msg in messages:
        assert creds not in msg
    assert any("REDACTED" in msg for msg in messages)


def test_redact_argv_exact_match_only_and_pure():
    cmd = ["skopeo", "copy", "--creds", "user:pass", "docker://x"]
    redacted = _redact_argv(cmd, ("user:pass",))
    assert redacted == ["skopeo", "copy", "--creds", "***REDACTED***", "docker://x"]
    # pure — the original list is untouched
    assert cmd == ["skopeo", "copy", "--creds", "user:pass", "docker://x"]


def test_redact_argv_no_redact_values_is_identity():
    cmd = ["skopeo", "copy", "docker://x"]
    assert _redact_argv(cmd, ()) == cmd


# ---------------------------------------------------------------------------
# 4. Digest-pinning cache-directory collision-safety (_oci_dir_for)
# ---------------------------------------------------------------------------


def test_oci_dir_for_two_different_digest_pins_are_distinct():
    """Baseline: two different full sha256 digest pins of the same repo were
    already distinct even under the OLD naive substitution scheme (the 64-hex
    digest passes through untouched) — must still hold after the fix."""
    store = Store(backend="plaindir", oci_cache_dir="/nonexistent-test-oci-dir")
    ref_a = "alpine@sha256:" + "a" * 64
    ref_b = "alpine@sha256:" + "b" * 64
    assert store._oci_dir_for(ref_a) != store._oci_dir_for(ref_b)


def test_oci_dir_for_resolves_a_real_separator_collision():
    """Reproduces a genuine collision the pre-fix _oci_dir_for had: a real
    digest pin `myrepo@sha256:<hex>` and a syntactically valid (if unusual)
    TAG reference `myrepo:sha256_<hex>` both sanitise to the IDENTICAL string
    under naive char-substitution (':', '/', '@' all collapse to the same
    '_'). The hash-suffix fix must keep them apart."""
    digest = "d" * 64
    ref_digest_pin = f"myrepo@sha256:{digest}"
    ref_lookalike_tag = f"myrepo:sha256_{digest}"

    # Sanity check: confirm these truly do collide under the old naive
    # substitution alone, otherwise this test would prove nothing.
    naive = lambda s: re.sub(r"[^a-zA-Z0-9._-]", "_", s)  # noqa: E731
    assert naive(ref_digest_pin) == naive(ref_lookalike_tag), (
        "test setup sanity check failed: these two refs no longer collide "
        "under naive substitution, so this test doesn't exercise the fix"
    )

    store = Store(backend="plaindir", oci_cache_dir="/nonexistent-test-oci-dir")
    assert store._oci_dir_for(ref_digest_pin) != store._oci_dir_for(ref_lookalike_tag)


def test_oci_dir_for_is_deterministic_across_calls():
    """Same image_ref must always map to the same directory (idempotent
    re-resolve of the same ref needs to keep landing in the same oci_dir)."""
    store = Store(backend="plaindir", oci_cache_dir="/nonexistent-test-oci-dir")
    ref = "debian:bookworm"
    assert store._oci_dir_for(ref) == store._oci_dir_for(ref)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_build_auth_args_none_given_no_env_is_unchanged,
    test_build_auth_args_creds_only,
    test_build_auth_args_authfile_param_only,
    test_build_auth_args_authfile_param_missing_file_is_silently_ignored,
    test_build_auth_args_env_var_fallback_when_neither_param_given,
    test_build_auth_args_authfile_param_beats_env_var,
    test_build_auth_args_creds_beats_authfile_param_and_env_var,
    test_resolve_no_auth_argv_unchanged,
    test_resolve_authfile_env_var_only,
    test_resolve_creds_param_only,
    test_resolve_both_given_creds_wins_precedence,
    test_run_redacts_creds_from_logs_on_success,
    test_run_redacts_creds_from_logs_and_exception_on_failure,
    test_redact_argv_exact_match_only_and_pure,
    test_redact_argv_no_redact_values_is_identity,
    test_oci_dir_for_two_different_digest_pins_are_distinct,
    test_oci_dir_for_resolves_a_real_separator_collision,
    test_oci_dir_for_is_deterministic_across_calls,
]


def run_all():
    failures = []
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            print(f"FAIL {fn.__name__}: {exc}")
            failures.append(fn.__name__)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            failures.append(fn.__name__)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"All {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

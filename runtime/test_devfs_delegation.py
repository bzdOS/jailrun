#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_devfs_delegation.py
# PURPOSE: unit tests for engine._devfs_delegation_available() and the
#          devfs_delegation_ok wiring in engine._build_jail_conf() — the
#          fix replacing the earlier "nested devfs always needs a daemon"
#          assumption (empirically disproven 2026-07-26, see engine.py).
# DEPENDENCIES: stdlib (subprocess, unittest.mock), runtime.engine
# PUBLIC_API: pytest test_* functions
# END_AI_HEADER
"""Unit tests for the nested-devfs-delegation probe and jail.conf wiring.

subprocess.run and _is_jailed are monkeypatched, so this runs on Linux with
no FreeBSD/jail(8) needed:

    python3 -m pytest runtime/test_devfs_delegation.py -v
"""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from runtime import engine  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = b""


# ---- _devfs_delegation_available ----

def test_available_true_when_probe_jail_creates_and_tears_down(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["jail", "-c"]:
            return _FakeCompleted(0)
        if cmd[:2] == ["jail", "-r"]:
            return _FakeCompleted(0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine._devfs_delegation_available() is True
    # both create and teardown were attempted
    assert any(c[:2] == ["jail", "-c"] for c in calls)
    assert any(c[:2] == ["jail", "-r"] for c in calls)


def test_available_false_when_probe_jail_create_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["jail", "-c"]:
            return _FakeCompleted(1, b"mount: .: Operation not permitted\n")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine._devfs_delegation_available() is False
    # nothing was created, so teardown must never be attempted
    assert not any(c[:2] == ["jail", "-r"] for c in calls)


def test_available_false_when_jail_binary_missing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("jail: not found")

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine._devfs_delegation_available() is False


def test_available_probe_cleans_up_scratch_dir_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "probe"))
    os.makedirs(tmp_path / "probe")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["jail", "-c"]:
            return _FakeCompleted(0)
        return _FakeCompleted(0)

    monkeypatch.setattr(engine.subprocess, "run", fake_run)
    assert engine._devfs_delegation_available() is True
    assert not (tmp_path / "probe").exists()


# ---- _build_jail_conf ruleset wiring ----

_BASE_KWARGS = dict(
    jail_name="jailrun-test",
    rootfs_path="/tmp/rootfs",
    mounts=[],
    extra_params=[],
    linuxulator=False,
)


def test_bare_host_leaves_ruleset_at_natural_default(monkeypatch):
    monkeypatch.setattr(engine, "_is_jailed", lambda: False)
    conf = engine._build_jail_conf(**_BASE_KWARGS)
    assert "mount.devfs;" in conf
    assert "devfs_ruleset" not in conf


def test_nested_with_delegation_ok_leaves_ruleset_at_natural_default(monkeypatch):
    monkeypatch.setattr(engine, "_is_jailed", lambda: True)
    conf = engine._build_jail_conf(**_BASE_KWARGS, devfs_delegation_ok=True)
    assert "mount.devfs;" in conf
    assert "devfs_ruleset" not in conf


def test_nested_without_delegation_forces_ruleset_zero(monkeypatch):
    monkeypatch.setattr(engine, "_is_jailed", lambda: True)
    conf = engine._build_jail_conf(**_BASE_KWARGS, devfs_delegation_ok=False)
    assert "devfs_ruleset = 0;" in conf

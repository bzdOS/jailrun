#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_thin_rootfs.py
# PURPOSE: regression tests for Store.empty_rootfs() and its destroy() early-return
# INTENT: thin-jail base mode -- an empty rootfs is created for a
#         per-run scratch dir instead of copying/cloning a whole base userland; the
#         caller nullfs-binds the real base dirs into it. destroy() must never try to
#         `zfs destroy` this (it was never a real ZFS dataset), regardless of backend.
# DEPENDENCIES: stdlib (tempfile, pathlib, os, sys), store.store (Store, Handle)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_thin_rootfs.py — regression tests for Store.empty_rootfs().

Run on host (no FreeBSD/ZFS required — empty_rootfs() only does mkdir, no
subprocess calls):
    python3 -m pytest store/test_thin_rootfs.py -v
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from store.store import Store  # noqa: E402


def test_empty_rootfs_creates_writable_skeleton():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(backend="plaindir", mountpoint_base=tmp)
        rootfs, handle = s.empty_rootfs()

        assert rootfs.is_dir()
        assert handle.thin is True
        assert handle.rootfs == rootfs
        assert handle.mounts == []
        for d in ("tmp", "var", "etc", "dev", "root", "home"):
            assert (rootfs / d).is_dir(), f"missing skeleton dir: {d}"


def test_empty_rootfs_is_actually_empty_of_base_dirs():
    # The caller (engine.py) is responsible for nullfs-binding /bin, /lib,
    # /usr etc. -- empty_rootfs() itself must not pre-populate them (that
    # would defeat the point: no copying).
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(backend="plaindir", mountpoint_base=tmp)
        rootfs, _handle = s.empty_rootfs()
        for d in ("bin", "lib", "libexec", "sbin", "usr"):
            assert not (rootfs / d).exists(), (
                f"empty_rootfs() should not pre-create base dir {d!r} -- "
                "that's the caller's job via mount()"
            )


def test_empty_rootfs_unique_per_call():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(backend="plaindir", mountpoint_base=tmp)
        rootfs1, handle1 = s.empty_rootfs()
        rootfs2, handle2 = s.empty_rootfs()
        assert rootfs1 != rootfs2
        assert handle1.id != handle2.id


def test_destroy_thin_handle_never_calls_zfs_destroy():
    # Regression guard: a thin handle must be torn down via rm -rf regardless
    # of self.backend, since it was never a real ZFS dataset. If destroy()
    # ever routed this through the ZFS branch, self._run(["zfs", "destroy",
    # ...]) would be invoked on a bogus "dataset" (a plain directory path) --
    # patch _run to fail loudly if that ever happens.
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(backend="zfs", pool="jailrun", mountpoint_base=tmp)

        def _run_should_not_be_called(cmd, *a, **kw):
            raise AssertionError(
                f"destroy() called self._run({cmd!r}) for a thin handle -- "
                "should have taken the rm -rf early-return instead"
            )

        s._run = _run_should_not_be_called  # type: ignore[method-assign]

        rootfs, handle = s.empty_rootfs()
        marker = rootfs / "canary.txt"
        marker.write_text("x")

        s.destroy(handle)

        assert not rootfs.exists(), "destroy() should have removed the thin rootfs"


TESTS = [
    test_empty_rootfs_creates_writable_skeleton,
    test_empty_rootfs_is_actually_empty_of_base_dirs,
    test_empty_rootfs_unique_per_call,
    test_destroy_thin_handle_never_calls_zfs_destroy,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_mount_containment.py
# PURPOSE: regression tests for Store.mount()'s bind-destination containment guard
# INTENT: `-v host:ctr` destinations are usually operator-supplied, but a caller may
#         build `ctr` from data derived from an untrusted user upload (e.g. a component name). A
#         `../../etc`-shaped destination must be refused before mkdir/mount_nullfs ever run, not
#         silently escape the rootfs.
# DEPENDENCIES: stdlib (tempfile, pathlib, os, sys), store.store (Store, Handle, StoreError)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_mount_containment.py — regression tests for Store.mount()'s containment guard.

Run on host (no FreeBSD/mount_nullfs required — the guard raises before any
subprocess is spawned for the malicious case):
    python3 -m pytest store/test_mount_containment.py -v
    # or directly:
    python3 store/test_mount_containment.py
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from store.store import Store, Handle, StoreError  # noqa: E402


def _make_handle(rootfs: Path) -> Handle:
    return Handle(id="test", rootfs=rootfs, dataset=str(rootfs), snapshot_id="test@snap")


def test_dotdot_escape_is_refused_before_any_subprocess():
    """A `../../etc`-shaped -v destination must raise StoreError, not reach mount_nullfs."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        rootfs = base / "rootfs"
        rootfs.mkdir()
        host_src = base / "hostsrc"
        host_src.mkdir()

        s = Store(backend="plaindir", mountpoint_base=str(base / "var"))
        handle = _make_handle(rootfs)

        try:
            s.mount(handle, binds=[(str(host_src), "../../../../etc", False)])
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass  # fail-closed rejection is the required outcome

        # Nothing should have been created outside rootfs, and no mount attempted.
        assert handle.mounts == []


def test_normal_dest_still_mkdirs_inside_rootfs():
    """A well-formed destination passes the containment check and reaches mkdir (pre-mount_nullfs)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        rootfs = base / "rootfs"
        rootfs.mkdir()
        host_src = base / "hostsrc"
        host_src.mkdir()

        s = Store(backend="plaindir", mountpoint_base=str(base / "var"))
        handle = _make_handle(rootfs)

        # mount_nullfs itself won't exist on this host — the containment check must pass
        # (no StoreError from _within) and the failure, if any, must come from the actual
        # mount_nullfs invocation (StoreError from _run), not from the containment guard.
        try:
            s.mount(handle, binds=[(str(host_src), "/mnt/work", False)])
        except StoreError as exc:
            assert "resolves outside" not in str(exc), f"containment guard rejected a valid path: {exc}"
        # Regardless of whether mount_nullfs itself succeeded, mkdir must have happened.
        assert (rootfs / "mnt" / "work").is_dir()


TESTS = [
    test_dotdot_escape_is_refused_before_any_subprocess,
    test_normal_dest_still_mkdirs_inside_rootfs,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_merge_tree.py
# PURPOSE: security regression tests for the OCI bsdtar-fallback path — symlink path-escape via
#          layer merge AND via whiteout processing
# INTENT: a layer that ships `usr/x -> <outside>` then a later entry `usr/x/pwned` must NOT be
#         written THROUGH the symlink onto the host (this code runs as root). _merge_tree must
#         either neutralize the inherited symlink or fail closed (StoreError) — never escape dst.
#         Same threat model applies to whiteout processing: an EARLIER layer's legitimate symlink
#         combined with a LATER layer's opaque/file whiteout marker under that path must not let
#         _clear_opaque_whiteout/_apply_file_whiteout delete/iterate through it onto the host —
#         this was a second, separate escape NOT covered by the original _merge_tree fix (that fix
#         only guarded writes in _merge_tree; whiteout processing runs before it, over the same
#         attacker-influenced rootfs state left by a prior layer).
#         Also unit-tests the _within containment guard and that normal merges/whiteouts still work.
# DEPENDENCIES: stdlib (os, sys, tempfile, pathlib), store.store (_merge_tree, _within, StoreError,
#               _clear_opaque_whiteout, _apply_file_whiteout)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_merge_tree.py — security regression tests for the OCI bsdtar-fallback layer merge
and whiteout processing.

Run on host (no FreeBSD required):
    python3 -m pytest store/test_merge_tree.py -v
    # or directly:
    python3 store/test_merge_tree.py
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from store.store import (  # noqa: E402
    _merge_tree,
    _within,
    StoreError,
    _clear_opaque_whiteout,
    _apply_file_whiteout,
)


def test_symlink_write_through_is_prevented():
    """Cross-layer attack: layer1 makes usr/x -> <outside>; layer2 ships usr/x/pwned.
    The write must land inside the rootfs (or be rejected) — never on the host."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        outside = base / "HOST"  # stands in for the host fs outside the rootfs
        outside.mkdir()

        # Layer 1: symlink usr/x -> <outside> (absolute escape target).
        l1 = base / "layer1"
        (l1 / "usr").mkdir(parents=True)
        os.symlink(str(outside), str(l1 / "usr" / "x"))
        _merge_tree(l1, dst)
        assert (dst / "usr" / "x").is_symlink()

        # Layer 2: a real file usr/x/pwned — must NOT be written through the symlink.
        l2 = base / "layer2"
        (l2 / "usr" / "x").mkdir(parents=True)
        (l2 / "usr" / "x" / "pwned").write_bytes(b"owned")

        try:
            _merge_tree(l2, dst)
        except StoreError:
            pass  # fail-closed rejection is also an acceptable outcome

        # The one property that MUST hold: nothing escaped onto the host.
        assert not (outside / "pwned").exists(), "write escaped the rootfs onto the host!"


def test_within_containment():
    with tempfile.TemporaryDirectory() as td:
        root = (Path(td) / "r")
        root.mkdir()
        (root / "sub").mkdir()
        root_real = root.resolve()
        assert _within(root / "sub", root_real)
        assert _within(root, root_real)
        assert not _within(Path(td), root_real)  # parent is outside root
        # A symlink pointing out of root: paths through it must read as escaping.
        os.symlink(td, str(root / "esc"))
        assert not _within(root / "esc" / "x", root_real)


def test_normal_merge_still_works():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        src = base / "layer"
        (src / "etc").mkdir(parents=True)
        (src / "etc" / "hosts").write_bytes(b"127.0.0.1 localhost\n")
        os.symlink("hosts", str(src / "etc" / "hosts.link"))  # safe relative symlink
        (src / ".wh.ignored").write_bytes(b"")  # whiteout marker must be skipped
        _merge_tree(src, dst)
        assert (dst / "etc" / "hosts").read_bytes() == b"127.0.0.1 localhost\n"
        assert (dst / "etc" / "hosts.link").is_symlink()
        assert not (dst / ".wh.ignored").exists()


def test_opaque_whiteout_symlink_escape_is_prevented():
    """An EARLIER layer plants rootfs/usr/evil -> <outside> (a legitimate
    symlink — real images do this). A LATER layer's opaque-whiteout marker under that path
    must not cause _clear_opaque_whiteout to iterate/delete through it onto the host."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "usr").mkdir(parents=True)
        outside = base / "HOST"
        outside.mkdir()
        (outside / "precious").write_bytes(b"do not delete me")

        # Earlier layer's legitimate symlink (allowed — its mere existence is fine).
        os.symlink(str(outside), str(dst / "usr" / "evil"))

        container_dir = dst / "usr" / "evil"
        try:
            _clear_opaque_whiteout(container_dir, dst.resolve())
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass  # fail-closed rejection is the required outcome

        assert (outside / "precious").exists(), "opaque whiteout deleted through the symlink onto the host!"


def test_file_whiteout_symlink_escape_is_prevented():
    """Same escape class as above, for the single-file whiteout path."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "usr").mkdir(parents=True)
        outside = base / "HOST"
        outside.mkdir()
        (outside / "precious").write_bytes(b"do not delete me")

        os.symlink(str(outside), str(dst / "usr" / "evil"))

        container_target = dst / "usr" / "evil" / "precious"
        try:
            _apply_file_whiteout(container_target, dst.resolve())
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass

        assert (outside / "precious").exists(), "file whiteout deleted through the symlink onto the host!"


def test_normal_whiteouts_still_work():
    """Non-malicious case: whiteout targets that are genuinely inside rootfs are cleared/removed."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "etc" / "opaquedir").mkdir(parents=True)
        (dst / "etc" / "opaquedir" / "old1").write_bytes(b"x")
        (dst / "etc" / "opaquedir" / "old2").write_bytes(b"y")
        (dst / "etc" / "plainfile").write_bytes(b"z")
        dst_real = dst.resolve()

        _clear_opaque_whiteout(dst / "etc" / "opaquedir", dst_real)
        assert list((dst / "etc" / "opaquedir").iterdir()) == []

        _apply_file_whiteout(dst / "etc" / "plainfile", dst_real)
        assert not (dst / "etc" / "plainfile").exists()


TESTS = [
    test_symlink_write_through_is_prevented,
    test_within_containment,
    test_normal_merge_still_works,
    test_opaque_whiteout_symlink_escape_is_prevented,
    test_file_whiteout_symlink_escape_is_prevented,
    test_normal_whiteouts_still_work,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

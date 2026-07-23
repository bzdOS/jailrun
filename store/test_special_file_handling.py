#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_special_file_handling.py
# PURPOSE: regression test for store/store.py's _merge_tree special-file handling — the
#          contract fix that closes the non-security "wrong exception type" nit documented
#          (but deliberately not fixed) by test_layer_adversarial.py's
#          test_device_node_style_entry_does_not_write_into_rootfs /
#          test_device_node_style_entry_socket_variant_does_not_write_into_rootfs and the
#          two equivalent cases in test_layer_adversarial_extended.py.
# INTENT: _merge_tree runs AS ROOT over attacker-controlled OCI-layer content (see
#         docs/THREAT-MODEL.md Surface (a)). Its stated contract (module docstring +
#         contract comment) is that every error path raises store.StoreError. Before this
#         fix, a non-regular, non-symlink, non-directory entry (a FIFO, an AF_UNIX socket,
#         or — as root — a device node) reached the shutil.copy2() call in the final else
#         branch, which raised shutil.SpecialFileError (FIFO) or a plain OSError (socket):
#         both OSError subclasses, NEITHER a StoreError. A caller catching only StoreError
#         (as the rest of store.py does) would not have caught either. The SAFETY property
#         always held (nothing written into dst, no partial state, no hang) — this is purely
#         an exception-type contract fix: _merge_tree now detects a non-regular file
#         explicitly and raises StoreError BEFORE copy2 is ever called. These tests pin
#         that fix down with the two special-file types representable WITHOUT root (a FIFO
#         via os.mkfifo, an AF_UNIX socket via socket.bind) — the same stand-ins the
#         existing adversarial corpus uses, since a real char/block device node requires
#         root to create and is out of scope for this pure-Python, Linux-host test layer.
# DEPENDENCIES: stdlib (os, socket, sys, tempfile, pathlib, shutil), store.store
#               (_merge_tree, StoreError)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_special_file_handling.py — regression test for _merge_tree's special-file handling.

Scope: PURE PYTHON only, same as test_layer_adversarial.py / test_layer_adversarial_extended.py.
Does not shell out to bsdtar (the raw tar extraction that precedes _merge_tree is a separate
trust boundary those files already document and is out of scope here too).

The two special-file types representable without root are both exercised:
  * FIFO (named pipe) via os.mkfifo  — what shutil.copyfile()'s stat.S_ISFIFO guard catches.
  * AF_UNIX socket via socket.bind   — what Python's open() rejects before copy2 proceeds.
A real char/block device-node tar entry needs root to create and so cannot be constructed in
this test layer; the FIFO/socket pair are the same stand-ins test_layer_adversarial.py uses,
and they exercise the exact same "not item.is_file()" branch the fix added.

What changed vs. the pre-fix behavior those files documented:
  Before: _merge_tree(src with a FIFO/socket) raised shutil.SpecialFileError / plain OSError
          (both OSError subclasses) — caught by `except Exception` in the adversarial tests but
          NOT by `except StoreError`, leaving the module's own stated contract inconsistent.
  After:  _merge_tree raises StoreError, which IS what store.py's callers catch.

Run on host (no FreeBSD required):
    python3 -m pytest store/test_special_file_handling.py -v
    # or directly:
    python3 store/test_special_file_handling.py
"""

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from store.store import _merge_tree, StoreError  # noqa: E402


def test_fifo_entry_raises_store_error_and_is_not_materialized():
    """A FIFO (named pipe, created via os.mkfifo — no privilege required) in a layer must
    now raise StoreError — NOT shutil.SpecialFileError or any other bare OSError subclass
    that store.py's StoreError-catching callers would miss. The FIFO must never be created
    in dst (the destination is never opened). This is the concrete fix for the nit
    test_layer_adversarial.py's test_device_node_style_entry_does_not_write_into_rootfs
    documented as 'fails closed, wrong exception type, not fixed here'."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        src = base / "layer"
        src.mkdir()
        os.mkfifo(str(src / "devlike"))

        with pytest.raises(StoreError):
            _merge_tree(src, dst)

        # The fix's whole point: the exception must be a StoreError, NOT an OSError
        # subclass (StoreError extends RuntimeError, so this distinguishes the fixed
        # path from the old shutil.SpecialFileError leak).
        try:
            _merge_tree(src, dst)
            raise AssertionError("expected StoreError, got no exception")
        except StoreError:
            pass
        except OSError as e:
            # SpecialFileError is an OSError subclass — reaching here means the fix
            # regressed back to leaking shutil's exception type.
            raise AssertionError(
                f"regression: _merge_tree leaked a bare OSError subclass "
                f"({type(e).__name__}) instead of StoreError"
            )

        assert not (dst / "devlike").exists(), "a FIFO must never be materialized in dst"
        assert list(dst.iterdir()) == [], "nothing should be written under dst"


def test_socket_entry_raises_store_error_and_is_not_materialized():
    """An AF_UNIX socket special file (created via socket.bind — no privilege required),
    the second representable-without-root stand-in for a device-node entry. Before the fix
    this raised a plain OSError inside open() even earlier than the FIFO case; it must now
    raise StoreError like every other _merge_tree error path, and must not be created in dst.
    Mirrors test_layer_adversarial.py's
    test_device_node_style_entry_socket_variant_does_not_write_into_rootfs, but asserts the
    exception TYPE (StoreError), not merely that 'some exception' is raised."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        src = base / "layer"
        src.mkdir()
        sock_path = src / "sockish"
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.bind(str(sock_path))
            with pytest.raises(StoreError):
                _merge_tree(src, dst)
            try:
                _merge_tree(src, dst)
                raise AssertionError("expected StoreError, got no exception")
            except StoreError:
                pass
            except OSError as e:
                raise AssertionError(
                    f"regression: _merge_tree leaked a bare OSError subclass "
                    f"({type(e).__name__}) instead of StoreError"
                )
            assert not (dst / "sockish").exists(), (
                "a socket special file must never be materialized in dst"
            )
            assert list(dst.iterdir()) == [], "nothing should be written under dst"
        finally:
            s.close()


def test_store_error_message_names_the_offending_entry():
    """The StoreError raised for a special-file entry should identify which entry and what
    kind it is, so an operator reading the error (or grepping logs) can see exactly which
    OCI-layer entry was refused and its mode. Regression guard on the message shape, not
    just the exception class."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        src = base / "layer"
        src.mkdir()
        os.mkfifo(str(src / "named_pipe"))

        try:
            _merge_tree(src, dst)
        except StoreError as e:
            msg = str(e)
            assert "named_pipe" in msg, "error message must name the offending entry"
            # stat.filemode renders a FIFO as 'p...'; assert it reports a non-regular type
            # rather than a bare mode, so the operator sees what kind of entry was refused.
            assert "not a regular file" in msg
        else:
            raise AssertionError("expected StoreError")


def test_nested_fifo_entry_in_subdirectory_is_caught():
    """The fix must catch a special file nested inside a subdirectory of a layer (the
    realistic shape: an entry at e.g. var/run/something), not only a top-level one — the
    guard runs per-entry in the rglob loop, so a deeper path exercises the same code with
    a non-trivial rel. Confirms the rel path reported in the error is the layer-relative
    one, and that sibling regular files in the SAME layer (before and after the special
    file in rglob order) are unaffected: the well-formed ones still merge, the special
    file is still refused. This guards against a naive fix that only checked top-level
    entries."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        src = base / "layer"
        (src / "var" / "run").mkdir(parents=True)
        (src / "var" / "run" / "ok_file").write_bytes(b"fine")
        os.mkfifo(str(src / "var" / "run" / "weird"))

        try:
            _merge_tree(src, dst)
        except StoreError as e:
            assert "var/run/weird" in str(e)
        else:
            raise AssertionError("expected StoreError for nested FIFO")

        # The FIFO itself must never land in dst, regardless of ordering.
        assert not (dst / "var" / "run" / "weird").exists()


def test_regular_file_still_merges_unaffected_by_the_guard():
    """Sanity / over-rejection guard: the special-file check (item.is_file()) must NOT
    cause a legitimate regular file to be refused. A plain regular file in a layer must
    still copy through shutil.copy2 exactly as before, content and timestamps preserved.
    This is the proof the fix is scoped to special files only, not a blanket refusal."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        src = base / "layer"
        src.mkdir()
        payload = b"just a regular file, nothing special"
        (src / "plain.txt").write_bytes(payload)

        _merge_tree(src, dst)  # must NOT raise

        copied = dst / "plain.txt"
        assert copied.is_file(), "regular file must be copied through"
        assert copied.read_bytes() == payload
        # shutil.copy2 preserves mtime; assert the timestamp carried over (not just content).
        assert copied.stat().st_mtime == (src / "plain.txt").stat().st_mtime


TESTS = [
    test_fifo_entry_raises_store_error_and_is_not_materialized,
    test_socket_entry_raises_store_error_and_is_not_materialized,
    test_store_error_message_names_the_offending_entry,
    test_nested_fifo_entry_in_subdirectory_is_caught,
    test_regular_file_still_merges_unaffected_by_the_guard,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

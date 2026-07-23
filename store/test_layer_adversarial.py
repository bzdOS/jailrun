#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_layer_adversarial.py
# PURPOSE: adversarial/corpus regression tests for the pure-Python OCI layer-extraction
#          primitives (_merge_tree / _clear_opaque_whiteout / _apply_file_whiteout / _within)
#          — the functions store/test_merge_tree.py already exercises, extended with a wider
#          adversarial corpus for jailrun's 0.6 milestone (privilege reduction & adversarial
#          campaign)
# INTENT: these functions run AS ROOT during `jailrun run` (see store.py's _unpack_bsdtar),
#         over content from an untrusted OCI image. test_merge_tree.py already proved two
#         single-hop symlink-escape fixes; this file goes looking for MORE escape shapes:
#         multi-hop symlink chains, relative (`../..`) escape targets (as opposed to absolute
#         host paths), hardlink games, plain (non-symlink) path traversal, and — where
#         representable without root — a device-node-style special-file entry (FIFO). None of
#         these is depended on to find a NEW hole; each is a targeted probe, and every one of
#         them is asserted to either raise StoreError (fail-closed) or be silently neutralized
#         by the pre-existing "never descend through an inherited symlink" defense in
#         _merge_tree — and, in all cases, to never write or delete anything outside the
#         destination rootfs. See the per-test docstrings for exactly which of those two
#         outcomes was empirically observed for that scenario (verified 2026-07-22 against this
#         checkout — see REPORT_ON_FINDINGS below for the one non-security nit found).
# REPORT_ON_FINDINGS: no new escape was found in this pass. One minor, NON-security gap was
#          surfaced and has SINCE BEEN FIXED: a FIFO (or, by the same code path, an AF_UNIX
#          socket, or — as root — a char/block device) entry made _merge_tree's shutil.copy2()
#          raise shutil.SpecialFileError (an OSError subclass), NOT store.StoreError. Nothing
#          was ever written into dst and nothing escaped — the safety property held — but a
#          caller catching only StoreError (the rest of store.py's contract) would not have
#          caught that particular crash. _merge_tree now raises StoreError on any non-regular-
#          file entry BEFORE copy2 is called (same fail-closed behavior, correct exception
#          type); the strict exception-type assertion lives in
#          store/test_special_file_handling.py. The two tests below keep their broad
#          `except Exception` (they assert the SAFETY property — nothing materializes in dst —
#          not the exception type), so they still pass unchanged and remain valid corpus
#          entries. See test_device_node_style_entry_does_not_write_into_rootfs below.
# DEPENDENCIES: stdlib (os, socket, sys, tempfile, pathlib), store.store (_merge_tree, _within,
#               StoreError, _clear_opaque_whiteout, _apply_file_whiteout)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_layer_adversarial.py — adversarial regression corpus for the bsdtar-fallback OCI
layer-extraction primitives (store/store.py's _merge_tree / _clear_opaque_whiteout /
_apply_file_whiteout / _within).

Scope: PURE PYTHON only. Does not shell out to bsdtar (not present on this host — see
ARCHITECTURE.md's "linux-host produces scaffold, not a validated runtime"). The bsdtar
invocation itself (raw extraction of a layer .tar.gz into a fresh temp dir, BEFORE
_merge_tree ever runs) is a separate, real trust boundary this file deliberately does NOT
exercise: whether libbsdtar's own extraction defends against a layer that plants a symlink
and then writes through it INSIDE THAT SAME EXTRACTION depends on the installed
libarchive's security-extraction defaults, which is host-specific and already marked
`# UNVERIFIED` in store.py. See docs/THREAT-MODEL.md surface (a) for the honest framing.

Run on host (no FreeBSD required):
    python3 -m pytest store/test_layer_adversarial.py -v
    # or directly:
    python3 store/test_layer_adversarial.py
"""

import os
import socket
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


def test_multi_hop_symlink_chain_write_through_is_neutralized():
    """Cross-layer, TWO-HOP chain: layer1 plants usr/a -> usr/b (relative, both nominally
    inside dst); a second merge plants usr/b -> <outside> (absolute escape target). A third
    layer then ships usr/a/pwned — Path.resolve() dereferences the WHOLE chain (not just one
    hop), so if _merge_tree's _within(target.parent, dst_real) guard ever fired here it would
    catch it. Empirically (verified against this checkout) it never gets that far: rglob()
    yields parent directories before their children (verified separately), so when the third
    layer's OWN "usr/a" entry is itself a directory, _merge_tree's existing "never treat an
    inherited symlink as a directory to descend into" branch (see _merge_tree's dir case)
    unlinks the stale usr/a symlink and replaces it with a REAL directory before "pwned" is
    ever copied — the write lands safely inside dst, not through the chain. Either outcome
    (StoreError OR silent neutralization) is acceptable; the one invariant that must hold is
    that nothing reaches outside the rootfs."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        outside = base / "HOST"
        outside.mkdir()

        # Hop 1: usr/a -> usr/b (relative, resolves inside dst at the time it's created).
        l1 = base / "layer1"
        (l1 / "usr").mkdir(parents=True)
        os.symlink("b", str(l1 / "usr" / "a"))
        _merge_tree(l1, dst)
        assert (dst / "usr" / "a").is_symlink()

        # Hop 2: usr/b -> <outside> (absolute escape target). Now usr/a -> usr/b -> outside.
        l1b = base / "layer1b"
        (l1b / "usr").mkdir(parents=True)
        os.symlink(str(outside), str(l1b / "usr" / "b"))
        _merge_tree(l1b, dst)
        assert (dst / "usr" / "b").is_symlink()

        # Attack layer: write through the two-hop chain.
        l2 = base / "layer2"
        (l2 / "usr" / "a").mkdir(parents=True)
        (l2 / "usr" / "a" / "pwned").write_bytes(b"owned")

        try:
            _merge_tree(l2, dst)
        except StoreError:
            pass  # fail-closed rejection is an acceptable outcome

        assert not (outside / "pwned").exists(), "write escaped the rootfs via a 2-hop symlink chain!"
        assert list(outside.iterdir()) == [], "nothing should ever have been written under HOST"


def test_relative_dotdot_symlink_escape_is_neutralized():
    """Same escape CLASS as test_merge_tree.py's existing single-hop test, but with a
    RELATIVE symlink target (`../../..`-shaped, computed via os.path.relpath so the test
    is portable across tempdir nesting depths) instead of an absolute host path. Real OCI
    layers ship relative symlinks far more often than absolute ones, and Path.resolve()
    must dereference relative targets against the symlink's OWN directory, not dst — a
    different code path inside resolve() than the absolute case test_merge_tree.py covers."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        outside = base / "HOST"
        outside.mkdir()

        l1 = base / "layer1"
        symlink_dir = l1 / "usr"
        symlink_dir.mkdir(parents=True)
        # Relative target computed from where the symlink will actually live once merged
        # (dst/usr), not from l1/usr — the merge re-homes it, and a real attacker would
        # craft the relative target for the FINAL location, not the staging tmp dir.
        rel_target = os.path.relpath(str(outside), str(dst / "usr"))
        os.symlink(rel_target, str(symlink_dir / "x"))
        _merge_tree(l1, dst)
        assert (dst / "usr" / "x").is_symlink()
        assert (dst / "usr" / "x").resolve() == outside.resolve(), "test setup sanity check"

        l2 = base / "layer2"
        (l2 / "usr" / "x").mkdir(parents=True)
        (l2 / "usr" / "x" / "pwned").write_bytes(b"owned")

        try:
            _merge_tree(l2, dst)
        except StoreError:
            pass

        assert not (outside / "pwned").exists(), "write escaped the rootfs via a relative ../.. symlink!"


def test_within_rejects_plain_dotdot_traversal_without_symlinks():
    """_within() must reject textual '..' path-traversal even with NO symlink involved at
    all — Path.resolve() collapses '..' components during normalization, independent of
    the symlink-following it also does. This is the ../ path-traversal case in isolation
    (test_mount_containment.py already covers the same class at the mount() call site;
    this asserts the underlying primitive directly)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "r"
        root.mkdir()
        root_real = root.resolve()
        assert _within(root / "sub_does_not_need_to_exist", root_real)
        assert not _within(root / ".." / "etc" / "passwd", root_real)
        assert not _within(Path(str(root) + "/../../etc"), root_real)
        assert not _within(root / ".." / ".." / ".." / "etc" / "shadow", root_real)


def test_hardlink_is_flattened_to_independent_copy_not_live_link():
    """Hardlink games: a layer can ship a tar entry that is a hardlink to another path.
    Simulated directly here with os.link() (bsdtar's own hardlink handling during raw
    extraction into its temp dir is the out-of-scope bsdtar shell-out — see module
    docstring). The property that matters at the _merge_tree layer: it must NEVER create a
    live hardlink INTO dst that aliases a path outside dst, because a live alias would let
    a later in-container write silently corrupt the host file sharing that inode.
    _merge_tree only ever uses shutil.copy2 (content copy) or os.symlink — never os.link —
    so this is expected to hold structurally; asserted here as a hard regression guard."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        outside = base / "HOST"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_bytes(b"host secret v1")

        src = base / "layer"
        (src / "etc").mkdir(parents=True)
        hardlinked = src / "etc" / "linked_secret"
        os.link(str(secret), str(hardlinked))
        assert secret.stat().st_ino == hardlinked.stat().st_ino  # sanity: real hardlink

        _merge_tree(src, dst)

        copied = dst / "etc" / "linked_secret"
        assert copied.exists()
        assert copied.read_bytes() == b"host secret v1"
        # The critical assertion: NOT the same inode as the host secret.
        assert copied.stat().st_ino != secret.stat().st_ino, (
            "merge_tree propagated a LIVE hardlink to a host path into the rootfs!"
        )

        # Mutating the in-container copy must not touch the host file.
        copied.write_bytes(b"attacker-controlled content from inside the container")
        assert secret.read_bytes() == b"host secret v1", "host file was corrupted through a shared hardlink!"


def test_opaque_whiteout_escape_via_nested_path_under_earlier_symlink():
    """Extends test_merge_tree.py's single-hop opaque-whiteout-under-symlink test: here the
    whiteout marker's container_dir is TWO levels BELOW the earlier layer's symlink (as
    _unpack_bsdtar would compute it for a marker nested inside a subdirectory reached only
    by walking through the symlink), not the symlinked path itself."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "var").mkdir(parents=True)
        outside = base / "HOST"
        outside.mkdir()
        (outside / "precious").write_bytes(b"do not delete me")

        os.symlink(str(outside), str(dst / "var" / "evil"))

        container_dir = dst / "var" / "evil" / "nested" / "deep"
        try:
            _clear_opaque_whiteout(container_dir, dst.resolve())
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass

        assert (outside / "precious").exists(), "nested opaque whiteout deleted through the symlink!"


def test_file_whiteout_escape_via_nested_path_under_earlier_symlink():
    """Same nested-path extension as above, for the single-file whiteout path."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "var").mkdir(parents=True)
        outside = base / "HOST"
        outside.mkdir()
        (outside / "precious").write_bytes(b"do not delete me")

        os.symlink(str(outside), str(dst / "var" / "evil"))

        container_target = dst / "var" / "evil" / "nested" / "precious"
        try:
            _apply_file_whiteout(container_target, dst.resolve())
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass

        assert (outside / "precious").exists(), "nested file whiteout deleted through the symlink!"


def test_opaque_whiteout_escape_via_symlink_chain():
    """A two-hop chain (var/a -> var/b -> outside) feeding an opaque whiteout target, not
    just a single-hop symlink — same rationale as the merge_tree multi-hop test above,
    applied to the whiteout path."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "var").mkdir(parents=True)
        outside = base / "HOST"
        outside.mkdir()
        (outside / "precious").write_bytes(b"do not delete me")

        os.symlink("b", str(dst / "var" / "a"))            # hop 1: relative, inside dst
        os.symlink(str(outside), str(dst / "var" / "b"))   # hop 2: absolute, escapes

        try:
            _clear_opaque_whiteout(dst / "var" / "a", dst.resolve())
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass

        assert (outside / "precious").exists(), "opaque whiteout deleted through a 2-hop symlink chain!"


def test_device_node_style_entry_does_not_write_into_rootfs():
    """Device-node-style / special-file entry, representable without root: a FIFO (named
    pipe, os.mkfifo — no privilege required, unlike a real char/block device node). A real
    OCI layer's tar can carry device-node entries (typeflag 3/4) which bsdtar would need
    root to actually create; this stands in for "some non-regular, non-symlink,
    non-directory filesystem entry lands in a layer."

    HISTORY (non-security; SINCE FIXED): _merge_tree's shutil.copy2() call had NO
    special-file guard of its own; shutil.copyfile() detected a FIFO specifically
    (stat.S_ISFIFO) and raised shutil.SpecialFileError immediately — verified empirically
    not to hang (the check happens before the destination is ever opened) — but that
    exception was a plain OSError subclass, NOT store.StoreError, so a caller catching
    only StoreError (as the rest of store.py's public contract implies) would NOT catch
    it. The SECURITY property this test asserts — nothing is written into dst, no partial
    state, no hang — held regardless of the exception's type, which is why this was a
    passing test and not an xfail. _merge_tree now raises StoreError on any non-regular-
    file entry before copy2 runs (see store/test_special_file_handling.py for the strict
    exception-type assertion); this test keeps its broad `except Exception` so it remains
    a valid corpus entry asserting the safety property directly. (The equivalent AF_UNIX-
    socket special file raised a plain OSError even earlier, for the same reason —
    Python's open() cannot open a socket as a regular file; covered by the variant test
    below.)
    """
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        src = base / "layer"
        src.mkdir()
        os.mkfifo(str(src / "devlike"))

        try:
            _merge_tree(src, dst)
        except Exception:
            pass  # some exception is expected here (see FINDING above) — never a hang

        assert not (dst / "devlike").exists(), "a special-file entry must never be materialized in dst"


def test_device_node_style_entry_socket_variant_does_not_write_into_rootfs():
    """Same class as above using an AF_UNIX socket special file instead of a FIFO — a
    second, independent representable-without-root stand-in for a device-node entry. Now
    fixed alongside the FIFO case (see store/test_special_file_handling.py); this test
    keeps its broad `except Exception` to assert the safety property (nothing materializes
    in dst) directly."""
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
            try:
                _merge_tree(src, dst)
            except Exception:
                pass  # some exception is expected (not StoreError) — never a hang, never a copy
            assert not (dst / "sockish").exists(), "a socket special file must never be materialized in dst"
        finally:
            s.close()


TESTS = [
    test_multi_hop_symlink_chain_write_through_is_neutralized,
    test_relative_dotdot_symlink_escape_is_neutralized,
    test_within_rejects_plain_dotdot_traversal_without_symlinks,
    test_hardlink_is_flattened_to_independent_copy_not_live_link,
    test_opaque_whiteout_escape_via_nested_path_under_earlier_symlink,
    test_file_whiteout_escape_via_nested_path_under_earlier_symlink,
    test_opaque_whiteout_escape_via_symlink_chain,
    test_device_node_style_entry_does_not_write_into_rootfs,
    test_device_node_style_entry_socket_variant_does_not_write_into_rootfs,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

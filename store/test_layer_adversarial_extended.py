#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_layer_adversarial_extended.py
# PURPOSE: SECOND extension of the adversarial/corpus regression suite for the pure-Python OCI
#          layer-extraction primitives (_merge_tree / _clear_opaque_whiteout / _apply_file_whiteout
#          / _within) — jailrun 0.6 milestone ("fuzz/corpus tests for layer parsing: whiteouts,
#          symlink/hardlink games, device nodes, path traversal"). test_layer_adversarial.py
#          already added 9 cases (multi-hop chains, relative-dotdot, hardlink flattening, nested
#          whiteout-under-symlink, device-node-style FIFO/socket); THIS file was written after
#          reading that file FULLY plus store/test_merge_tree.py (the original 6-case suite) so it
#          adds ONLY cases neither file already covers.
# INTENT: probe shapes those two files do not: (a) symlink chains an order of magnitude deeper
#          than the existing 2-hop tests, on BOTH _merge_tree's structural neutralization AND the
#          whiteout functions' raw _within resolution, to rule out unbounded recursion / resource
#          blowup; (b) a classic naive-string-prefix containment bug (root="…/rootfs" vs a sibling
#          "…/rootfs-evil") to prove _within's real Path.parents-based check isn't fooled by it —
#          not otherwise exercised by the existing dotdot-only _within test; (c) whiteout markers
#          for OCI-layer paths that never made it into the destination (a whiteout for something
#          that doesn't exist is a routine, spec-legal no-op — test_merge_tree.py's
#          test_normal_whiteouts_still_work only exercises EXISTING targets) — including the
#          type-confused sub-case where the "directory" an opaque whiteout expects to clear turns
#          out to be a plain file (a realistic malformed/adversarial layer shape: an OCI opaque
#          whiteout is spec-defined only for directories); (d) a deeply-nested (non-symlink)
#          directory structure through _merge_tree, as a plain robustness/resource check distinct
#          from the security-escape cases. A single-component filename over the host filesystem's
#          own name-length limit and an embedded-NUL-byte filename were BOTH considered and ruled
#          OUT as untestable at this layer: both are rejected by the OS/Python itself before a
#          source-side file with that name could even be created on disk, so bsdtar's own (already
#          out-of-scope, per test_layer_adversarial.py's docstring) extraction step would already
#          have failed on such an entry before _merge_tree ever saw it — see the "NOT TESTED" note
#          in the module docstring below for the empirical basis.
# REPORT_ON_FINDINGS: no new escape and no non-fail-closed crash found. TWO more instances of the
#          non-security "wrong exception type" nit class (an uncaught-by-callers-of-StoreError
#          exception type, with the safety property — nothing written or deleted outside the
#          intended scope, no partial state, no hang — fully intact) were found on two DIFFERENT
#          code paths here. NOTE: the ORIGINAL instance of this nit class — the FIFO/socket entry
#          in _merge_tree that test_layer_adversarial.py first documented — has SINCE BEEN FIXED
#          (_merge_tree now raises StoreError on any non-regular-file entry before copy2; see
#          store/test_special_file_handling.py). The two instances below are on the
#          _clear_opaque_whiteout code path, NOT _merge_tree, and so remain open here (a separate
#          fix, not part of that change):
#            1. _clear_opaque_whiteout on a container_dir that does not exist at all raises
#               FileNotFoundError (iterdir() on a missing path), not StoreError, before deleting
#               anything. In production this exact path is unreachable — _unpack_bsdtar's real
#               call site guards with `if container_dir.exists():` first — but the pure function
#               itself makes no such promise.
#            2. _clear_opaque_whiteout on a container_dir that DOES exist but is a plain file (not
#               a directory — e.g. an earlier layer shipped a regular file at that path, and a
#               later, malformed/adversarial layer's `.wh..wh..opq` marker targets it as if it
#               were a directory) raises NotADirectoryError (iterdir() on a non-directory), not
#               StoreError. Unlike case 1, this one IS reachable from the real call site:
#               Path.exists() returns True for a plain file too, so _unpack_bsdtar's own guard does
#               NOT filter it out. Still fails cleanly before any deletion — no escape, no partial
#               state — so it is documented here as a passing test (matching how
#               test_layer_adversarial.py treated the now-fixed FIFO/socket case), not an xfail: the one
#               thing that would make it an xfail-worthy finding — an actual escape, or a crash
#               that leaves inconsistent/partially-mutated state — does not happen here.
# DEPENDENCIES: stdlib (os, sys, tempfile, pathlib), store.store (_merge_tree, _within, StoreError,
#               _clear_opaque_whiteout, _apply_file_whiteout)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_layer_adversarial_extended.py — second extension of the adversarial regression corpus for
the bsdtar-fallback OCI layer-extraction primitives (store/store.py's _merge_tree /
_clear_opaque_whiteout / _apply_file_whiteout / _within).

Scope: PURE PYTHON only, same as store/test_layer_adversarial.py and store/test_merge_tree.py —
does not shell out to bsdtar (see test_layer_adversarial.py's module docstring for the full
rationale; that trust-boundary caveat applies identically here and is not repeated per-test).

NOT TESTED (checked, found unconstructible on this host, not merely assumed):
  * Embedded NUL byte in a filename — os.symlink/Path.mkdir/open all raise a Python-level
    ValueError ("embedded null byte") before any syscall happens; no real POSIX filename can
    contain NUL, so no on-disk source entry with this shape can ever exist for _merge_tree's
    src.rglob() to walk over in the first place.
  * A single path COMPONENT longer than the host filesystem's own name-length limit (confirmed
    empirically: creating a ~300-char single-component *source* entry on this tmpfs/ext4 host
    raises OSError "File name too long" at file-creation time, i.e. before it could ever become a
    _merge_tree source item). A real OCI tar can encode such a name via GNU/pax long-name
    extensions, but extracting it onto any real filesystem with the ordinary 255-byte-per-
    component limit fails inside bsdtar's own (separately out-of-scope) extraction step, so it
    never reaches _merge_tree either. A many-*component*, ordinary-length-per-component deep tree
    is fully constructible, though, and IS covered below
    (test_deeply_nested_directory_structure_merges_without_crash).

Run on host (no FreeBSD required):
    python3 -m pytest store/test_layer_adversarial_extended.py -v
    # or directly:
    python3 store/test_layer_adversarial_extended.py
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


def test_deep_symlink_chain_merge_tree_neutralized_at_depth():
    """80-hop symlink chain (link_0 -> link_1 -> ... -> link_79 -> <outside>, absolute final
    hop), roughly 40x deeper than test_layer_adversarial.py's 2-hop multi-hop test — built
    directly on disk (not via 80 separate _merge_tree calls) so the test stays fast while still
    exercising the real depth. A later layer then ships usr/link_0/pwned, i.e. tries to write
    THROUGH the full chain. Purpose: rule out unbounded recursion / resource blowup in
    _merge_tree's own resolution at a depth well beyond anything the existing suite exercises,
    and confirm the SAME "never treat an inherited symlink as a directory to descend into"
    neutralization documented in test_layer_adversarial.py's 2-hop test holds regardless of
    chain depth (empirically verified up to 300 hops during test authorship; 80 is used here
    to keep runtime negligible while remaining a two-orders-of-magnitude-deeper probe than the
    existing tests)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        outside = base / "HOST"
        outside.mkdir()
        (dst / "usr").mkdir()

        depth = 80
        for i in range(depth - 1):
            os.symlink(f"link_{i + 1}", str(dst / "usr" / f"link_{i}"))
        os.symlink(str(outside), str(dst / "usr" / f"link_{depth - 1}"))
        # Sanity: the chain really does resolve all the way out (test setup check, not the
        # property under test).
        assert (dst / "usr" / "link_0").resolve() == outside.resolve()

        attack = base / "layer2"
        (attack / "usr" / "link_0").mkdir(parents=True)
        (attack / "usr" / "link_0" / "pwned").write_bytes(b"owned")

        try:
            _merge_tree(attack, dst)
        except StoreError:
            pass  # fail-closed rejection is also an acceptable outcome

        assert not (outside / "pwned").exists(), (
            "write escaped the rootfs through an 80-hop symlink chain!"
        )
        assert list(outside.iterdir()) == [], "nothing should ever have been written under HOST"


def test_deep_symlink_chain_whiteout_fails_closed_at_depth():
    """Same 80-hop chain shape as above, but feeding _apply_file_whiteout / _clear_opaque_whiteout
    directly instead of _merge_tree. This matters because _merge_tree's neutralization above works
    structurally (it replaces the chain's head with a real directory before ever resolving deeper
    — see that test's docstring) and so never actually forces _within to walk the full 80-hop
    chain; the whiteout functions have no such structural escape hatch — they call _within (i.e.
    Path.resolve()) directly on the caller-supplied path, so THIS is the test that actually
    exercises deep-chain resolution end to end. Both whiteout entry points are checked."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "var").mkdir(parents=True)
        outside = base / "HOST"
        outside.mkdir()
        (outside / "precious").write_bytes(b"do not delete me")
        dst_real = dst.resolve()

        depth = 80
        for i in range(depth - 1):
            os.symlink(f"link_{i + 1}", str(dst / "var" / f"link_{i}"))
        os.symlink(str(outside), str(dst / "var" / f"link_{depth - 1}"))

        try:
            _apply_file_whiteout(dst / "var" / "link_0" / "precious", dst_real)
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass
        assert (outside / "precious").exists(), (
            "file whiteout deleted through an 80-hop symlink chain!"
        )

        try:
            _clear_opaque_whiteout(dst / "var" / "link_0", dst_real)
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass
        assert (outside / "precious").exists(), (
            "opaque whiteout deleted through an 80-hop symlink chain!"
        )


def test_within_rejects_sibling_directory_whose_name_shares_root_as_string_prefix():
    """Classic containment-check bug class, NOT exercised by the existing '..'-only _within test:
    a naive `str(candidate).startswith(str(root))` containment check would wrongly accept a
    SIBLING directory whose name merely happens to start with root's own name (e.g. root
    '.../rootfs' vs sibling '.../rootfs-evil') — no '..' or symlink involved at all, just a
    string-prefix false positive. _within is implemented via Path.resolve() equality/`.parents`
    membership (component-wise), not string prefixing, so it must reject this cleanly; asserted
    directly here as a hard regression guard on the actual containment primitive."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "rootfs"
        root.mkdir()
        root_real = root.resolve()
        sibling = base / "rootfs-evil"
        sibling.mkdir()

        # Demonstrate the naive check WOULD be fooled (documents why this case matters)...
        assert str(sibling).startswith(str(root)), "test setup sanity check"
        # ...but the real implementation is not.
        assert not _within(sibling, root_real)
        assert not _within(sibling / "anything", root_real)
        assert _within(root / "sibling-but-really-inside", root_real)  # sanity: real descendant ok


def test_file_whiteout_via_symlink_into_prefix_sibling_directory_is_neutralized():
    """Concrete end-to-end consequence of the string-prefix case above: an earlier layer's
    legitimate symlink rootfs/link -> <sibling '-evil' dir that string-prefix-aliases rootfs's own
    name> (as opposed to the existing tests' unrelated 'HOST' escape target, which shares no name
    overlap with dst at all), then a later layer's file-whiteout marker targets a file through
    that link. Must fail closed exactly like the existing not-name-related escape tests."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        decoy = base / "rootfs-evil"
        decoy.mkdir()
        (decoy / "precious").write_bytes(b"do not delete me")
        dst_real = dst.resolve()

        os.symlink(str(decoy), str(dst / "link"))

        try:
            _apply_file_whiteout(dst / "link" / "precious", dst_real)
            raise AssertionError("expected StoreError (fail-closed), got no exception")
        except StoreError:
            pass
        assert (decoy / "precious").exists(), (
            "file whiteout deleted through a symlink into a name-prefix-sibling directory!"
        )


def test_file_whiteout_on_nonexistent_target_is_safe_noop():
    """Milestone-requested case: a whiteout marker (.wh.<name>) whose target never made it into
    the destination at all — e.g. an earlier layer never shipped the file, or it was already
    removed by an even-earlier whiteout. This is routine and spec-legal (bsdtar/_unpack_bsdtar
    doesn't know in advance whether a whiteout target exists), and test_merge_tree.py's
    test_normal_whiteouts_still_work only covers whiteouts against targets that DO exist — this
    fills that gap for the file-whiteout path. Must be a true no-op: no exception, and the
    destination tree is byte-for-byte unchanged."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "var").mkdir(parents=True)
        dst_real = dst.resolve()

        before = sorted(str(p.relative_to(dst)) for p in dst.rglob("*"))
        _apply_file_whiteout(dst / "var" / "never_existed", dst_real)
        after = sorted(str(p.relative_to(dst)) for p in dst.rglob("*"))

        assert before == after, "a no-op whiteout must not change the destination tree at all"
        assert not (dst / "var" / "never_existed").exists()


def test_opaque_whiteout_on_nonexistent_container_dir_documents_exception_type():
    """Opaque-whiteout counterpart to the no-op test above: an opaque-whiteout marker
    (.wh..wh..opq) for a directory that was never created in the destination at all.

    FINDING (non-security, same nit CLASS as test_layer_adversarial.py's FIFO/socket case
    — see this module's REPORT_ON_FINDINGS header — STILL OPEN here, unlike the FIFO/socket
    case which has since been fixed in _merge_tree; see store/test_special_file_handling.py):
    unlike the file-whiteout path (a true no-op, see the previous test), _clear_opaque_whiteout
    calls container_dir.iterdir() unconditionally once the containment check passes, and
    Path.iterdir() on a path that does not exist at all raises FileNotFoundError, not StoreError.
    In production this exact path is unreachable — _unpack_bsdtar's own call site only invokes
    _clear_opaque_whiteout after checking `container_dir.exists()` — but the pure function itself
    makes no such promise, so the same "caller catching only StoreError won't catch this" caveat
    applies here. The security property that matters — no deletion happens, nothing outside dst
    is touched — holds regardless, which is why this is a passing test (broad except) rather
    than an xfail."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "var").mkdir(parents=True)
        dst_real = dst.resolve()

        container_dir = dst / "var" / "never_existed_dir"
        try:
            _clear_opaque_whiteout(container_dir, dst_real)
        except Exception:
            pass  # some exception is expected here (see FINDING above) — never a hang, never a delete
        assert not container_dir.exists(), "a no-op opaque whiteout must not create anything"
        assert list((dst / "var").iterdir()) == [], "nothing should have been created under var"


def test_opaque_whiteout_on_plain_file_container_dir_documents_exception_type():
    """Type-confusion counterpart: an opaque-whiteout marker (.wh..wh..opq) targeting a path that
    DOES exist in the destination but is a plain FILE, not a directory (a realistic
    malformed/adversarial-layer shape: the OCI spec defines opaque whiteouts only for
    directories, so a crafted or corrupt layer claiming one over a file is squarely the kind of
    fuzz/corpus input this milestone calls for).

    FINDING (non-security, same class as above and as the now-fixed FIFO/socket nit — STILL
    OPEN here, on the _clear_opaque_whiteout code path; the FIFO/socket fix in _merge_tree
    does not cover this): container_dir.iterdir() on a path that exists but is a regular file
    raises NotADirectoryError, not StoreError. UNLIKE the nonexistent-directory case above,
    this one IS reachable from the real call site: Path.exists() returns True for a plain
    file, so _unpack_bsdtar's `if container_dir.exists():` guard does NOT filter this out
    before calling _clear_opaque_whiteout. The safety property still holds — the file is never
    deleted (iterdir() raises before any _rm_rf call), nothing outside dst is touched, no
    partial state — so this remains a passing/documenting test rather than an xfail: the file
    survives untouched, which is exactly what "fails closed" requires here, just with the wrong
    exception type."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        (dst / "var").mkdir(parents=True)
        dst_real = dst.resolve()

        container_dir = dst / "var" / "thing"
        container_dir.write_bytes(b"i am a file, not a directory")

        try:
            _clear_opaque_whiteout(container_dir, dst_real)
        except Exception:
            pass  # some exception is expected here (see FINDING above) — never a hang, never a delete
        assert container_dir.is_file(), "the file must survive untouched"
        assert container_dir.read_bytes() == b"i am a file, not a directory"


def test_deeply_nested_directory_structure_merges_without_crash():
    """Plain robustness/resource case (not a security-escape probe): a ~150-level-deep, ordinary
    (non-symlink) directory tree — an order of magnitude deeper than any nesting the existing
    suite constructs — merged via _merge_tree. Confirms no recursion-limit crash and no
    pathological slowdown, and that the deeply-nested leaf lands at the CORRECT path inside dst
    (not, say, silently truncated or misplaced), while nothing appears outside dst either."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        dst = base / "rootfs"
        dst.mkdir()
        outside = base / "HOST"
        outside.mkdir()
        src = base / "layer"

        depth = 150
        p = src
        for i in range(depth):
            p = p / f"d{i}"
        p.mkdir(parents=True)
        (p / "leaf.txt").write_bytes(b"deep file")

        _merge_tree(src, dst)

        expected = dst
        for i in range(depth):
            expected = expected / f"d{i}"
        assert (expected / "leaf.txt").read_bytes() == b"deep file", (
            "deeply nested entry did not land at the expected path inside dst"
        )
        assert list(outside.iterdir()) == [], "nothing should ever have been written under HOST"


TESTS = [
    test_deep_symlink_chain_merge_tree_neutralized_at_depth,
    test_deep_symlink_chain_whiteout_fails_closed_at_depth,
    test_within_rejects_sibling_directory_whose_name_shares_root_as_string_prefix,
    test_file_whiteout_via_symlink_into_prefix_sibling_directory_is_neutralized,
    test_file_whiteout_on_nonexistent_target_is_safe_noop,
    test_opaque_whiteout_on_nonexistent_container_dir_documents_exception_type,
    test_opaque_whiteout_on_plain_file_container_dir_documents_exception_type,
    test_deeply_nested_directory_structure_merges_without_crash,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

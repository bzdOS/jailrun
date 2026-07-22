#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_store_concurrency.py
# PURPOSE: regression tests for ROADMAP 0.5 "Concurrency" — per-image resolve()/unpack()
#          locking (Store._image_lock) and destroy()'s ordered, deepest-first-unmount
#          teardown (replacing the old 10-attempt retry-with--f loop)
# INTENT: (1) prove Store._image_lock actually serializes two callers racing the SAME
#         image key using REAL threads (fcntl.flock is per-open-file-description, so two
#         independent os.open() calls on the same lock path genuinely block each other
#         even within one process — see _image_lock's rationale in store.py); (2) prove
#         two DIFFERENT image keys never block each other (no global lock in disguise);
#         (3) prove the lock is released even when the locked body raises; (4) prove
#         handle.mounts tears down deepest-mount-first (not just reversed insertion
#         order) via the pure _mounts_deepest_first helper; (5) prove destroy() actually
#         drives that order in practice and never touches `zfs destroy` before every
#         unmount has been attempted; (6) an end-to-end resolve()+unpack() race for the
#         SAME image_ref across two Store instances (simulating two concurrent
#         `jailrun run` processes) — real threads, monkeypatched subprocess helpers
#         (this repo's established pattern, see runtime/test_gc.py) standing in for
#         skopeo/zfs — proving the second caller's redundant work is safely skipped
#         (unpack()'s existing snapshot-exists idempotency check) rather than racing
#         the dataset create.
# DEPENDENCIES: stdlib (hashlib, json, os, sys, tempfile, threading, time, pathlib),
#               store.store (Store, Handle, StoreError, _mounts_deepest_first)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_store_concurrency.py — regression tests for per-image locking and ordered teardown.

Run on host (no FreeBSD/ZFS required — subprocess calls are monkeypatched, following
the same pattern runtime/test_gc.py uses for runtime.gc._run_ok):
    python3 -m pytest store/test_store_concurrency.py -v
    # or directly:
    python3 store/test_store_concurrency.py
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from store.store import (  # noqa: E402
    Store,
    Handle,
    StoreError,
    _mounts_deepest_first,
)

import fcntl  # noqa: E402  (stdlib; POSIX-only, same constraint store.py itself has)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_handle(rootfs: Path, dataset: str, mounts=None, jail_name=None) -> Handle:
    return Handle(
        id="test",
        rootfs=rootfs,
        dataset=dataset,
        snapshot_id="test@snap",
        mounts=mounts if mounts is not None else [],
        jail_name=jail_name,
    )


def _seed_fake_oci_layout(oci_dir: Path) -> None:
    """Write a minimal-but-valid single-manifest OCI layout with zero layers.

    Sufficient for _compute_image_id / _find_manifest_for_tag / _find_oci_for_image_id
    and for _unpack_bsdtar (an empty `layers` list means its extraction loop is a
    real, harmless no-op — no bsdtar binary required).
    """
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


# ---------------------------------------------------------------------------
# 1. Store._image_lock — SAME key serializes across real threads
# ---------------------------------------------------------------------------


def test_same_image_lock_serializes_across_threads():
    """Two threads locking the SAME image key must never be inside the critical
    section at the same time — proven with real threading.Thread (not sequential
    calls), each acquiring its own fd via _image_lock's own os.open()."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(backend="plaindir", oci_cache_dir=str(Path(td) / "oci"))
        intervals: list[tuple[float, float]] = []
        lock_for_intervals = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()  # both threads attempt to enter near-simultaneously
            with store._image_lock("same-image:tag"):
                start = time.monotonic()
                time.sleep(0.2)
                end = time.monotonic()
            with lock_for_intervals:
                intervals.append((start, end))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not any(t.is_alive() for t in threads), "a thread hung — lock never released?"

        assert len(intervals) == 2
        (s1, e1), (s2, e2) = intervals
        # Disjoint intervals: one critical section must fully finish before the
        # other starts. Overlap would mean flock did NOT serialize them.
        overlap = max(s1, s2) < min(e1, e2)
        assert not overlap, f"same-image critical sections overlapped: {intervals}"


# ---------------------------------------------------------------------------
# 2. Store._image_lock — DIFFERENT keys do NOT serialize
# ---------------------------------------------------------------------------


def test_different_image_locks_do_not_serialize():
    """Two threads locking DIFFERENT image keys must be able to overlap in time —
    proves the lock is keyed per-image, not a global lock in disguise."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(backend="plaindir", oci_cache_dir=str(Path(td) / "oci"))
        intervals: dict[str, tuple[float, float]] = {}
        lock_for_intervals = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(key: str):
            barrier.wait()
            with store._image_lock(key):
                start = time.monotonic()
                time.sleep(0.3)
                end = time.monotonic()
            with lock_for_intervals:
                intervals[key] = (start, end)

        threads = [
            threading.Thread(target=worker, args=("image-a",)),
            threading.Thread(target=worker, args=("image-b",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not any(t.is_alive() for t in threads)

        assert len(intervals) == 2
        (s1, e1) = intervals["image-a"]
        (s2, e2) = intervals["image-b"]
        overlap = max(s1, s2) < min(e1, e2)
        assert overlap, f"different-image locks serialized when they should not have: {intervals}"


# ---------------------------------------------------------------------------
# 3. Store._image_lock — released even when the locked body raises
# ---------------------------------------------------------------------------


def test_lock_released_on_exception():
    """A locked body that raises must still release the flock (try/finally)."""
    with tempfile.TemporaryDirectory() as td:
        store = Store(backend="plaindir", oci_cache_dir=str(Path(td) / "oci"))
        key = "boom-image"

        try:
            with store._image_lock(key):
                raise ValueError("simulated failure inside the locked section")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError to propagate out of _image_lock")

        # Prove the lock is actually free: a fresh, independent fd on the SAME
        # lock file must acquire LOCK_EX | LOCK_NB immediately (no BlockingIOError).
        lock_path = Path(td) / "oci" / "locks" / f"{key}.lock"
        assert lock_path.exists()
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AssertionError("lock was still held after the body raised — leak")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        # And _image_lock itself can acquire the same key again afterwards.
        with store._image_lock(key):
            pass


# ---------------------------------------------------------------------------
# 4. _mounts_deepest_first — pure ordering helper
# ---------------------------------------------------------------------------


def test_mounts_deepest_first_orders_by_actual_depth_not_insertion_order():
    """Insertion order is deliberately scrambled relative to nesting depth (the
    OLD `reversed(handle.mounts)` approach would get this wrong) — the fix must
    still produce a deepest-first order derived from dest_path's own depth."""
    root = Path("/tmp/rootfs")
    shallow = (Path("/host/a"), root / "mnt", False)                 # depth: rootfs+1
    deep = (Path("/host/c"), root / "mnt" / "a" / "b", False)        # depth: rootfs+3
    mid = (Path("/host/b"), root / "mnt" / "a", False)               # depth: rootfs+2

    # Insertion order intentionally NOT nesting order: shallow, deep, mid.
    mounts = [shallow, deep, mid]
    ordered = _mounts_deepest_first(mounts)

    assert ordered == [deep, mid, shallow], (
        "expected strictly deepest-first order derived from path depth, got: "
        f"{ordered}"
    )


def test_mounts_deepest_first_ties_keep_reverse_insertion_order():
    """Equal-depth mounts should keep the same relative order the old plain
    reversed(handle.mounts) produced (a harmless, backward-compatible tiebreak)."""
    root = Path("/tmp/rootfs")
    m1 = (Path("/host/1"), root / "a", False)
    m2 = (Path("/host/2"), root / "b", False)
    m3 = (Path("/host/3"), root / "c", False)

    ordered = _mounts_deepest_first([m1, m2, m3])
    assert ordered == [m3, m2, m1]


# ---------------------------------------------------------------------------
# 5. destroy() — ordered teardown: unmounts (deepest-first) strictly BEFORE
#    zfs destroy, using this repo's monkeypatch-the-subprocess-helper pattern
#    (see runtime/test_gc.py's gc_module._run_ok fakes).
# ---------------------------------------------------------------------------


def test_destroy_unmounts_deepest_first_then_zfs_destroy_once():
    """destroy() must: (a) call `jail -r` first if jail_name is set, (b) unmount
    every bind, deepest dest_path first, (c) only then call `zfs destroy`, exactly
    once (no retry needed when everything unmounted cleanly on the first try)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        rootfs = base / "rootfs"
        rootfs.mkdir()

        store = Store(backend="zfs", pool="testpool", oci_cache_dir=str(base / "oci"))

        calls: list[tuple] = []

        def fake_run(cmd, cwd=None, input_=None, timeout=None, env=None):  # noqa: ARG001
            calls.append(("run", tuple(cmd)))
            return None  # callers never use the return value on this path

        def fake_run_ok(cmd, cwd=None, timeout=None):  # noqa: ARG001
            calls.append(("run_ok", tuple(cmd)))
            return 0, b"", b""  # every umount/jail -r "succeeds" on the first try

        store._run = fake_run
        store._run_ok = fake_run_ok

        # Deliberately scrambled insertion order (shallow before deep) so a bug
        # in ordering would show up as a wrong sequence below, not by accident.
        shallow_dest = rootfs / "jailrun-native" / "base"
        deep_dest = rootfs / "mnt" / "work" / "nested"
        mounts = [
            (Path("/host/base"), shallow_dest, True),
            (Path("/host/nested"), deep_dest, False),
        ]
        handle = _make_handle(
            rootfs, dataset="testpool/runs/abc123", mounts=mounts, jail_name="jailrun-abc123"
        )

        store.destroy(handle)

        # 1. jail -r happened, and happened first.
        assert calls[0] == ("run_ok", ("jail", "-r", "jailrun-abc123"))

        # 2. Both umounts happened, deepest dest_path first.
        umount_calls = [c for kind, c in calls if kind == "run_ok" and c[0] == "umount"]
        assert umount_calls == [
            ("umount", str(deep_dest)),
            ("umount", str(shallow_dest)),
        ], f"unmount order wrong: {umount_calls}"

        # 3. zfs destroy happened exactly once, and strictly AFTER every umount.
        zfs_destroy_indices = [i for i, (kind, c) in enumerate(calls) if c[:2] == ("zfs", "destroy")]
        umount_indices = [i for i, (kind, c) in enumerate(calls) if c[0] == "umount"]
        assert len(zfs_destroy_indices) == 1, f"expected exactly one zfs destroy, got: {calls}"
        assert max(umount_indices) < zfs_destroy_indices[0], (
            "zfs destroy was attempted before all unmounts completed: " f"{calls}"
        )
        # No retry / no -f needed since every _run_ok call above returned rc=0.
        assert calls[zfs_destroy_indices[0]][1] == ("zfs", "destroy", "testpool/runs/abc123")

        # handle.mounts was drained.
        assert handle.mounts == []


def test_destroy_plaindir_backend_still_unmounts_before_removing_rootfs():
    """The plaindir backend has no zfs-destroy retry concept, but destroy() must
    still unmount every bind before the directory is rm -rf'd."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        rootfs = base / "runs" / "run1"
        (rootfs / "mnt" / "nested").mkdir(parents=True)
        rootfs_marker = rootfs / "keepme.txt"
        rootfs_marker.write_text("data")

        store = Store(backend="plaindir", mountpoint_base=str(base / "var"), oci_cache_dir=str(base / "oci"))

        calls: list[tuple] = []

        def fake_run_ok(cmd, cwd=None, timeout=None):  # noqa: ARG001
            calls.append(tuple(cmd))
            return 0, b"", b""

        store._run_ok = fake_run_ok

        mounts = [
            (Path("/host/x"), rootfs / "mnt", False),
            (Path("/host/y"), rootfs / "mnt" / "nested", False),
        ]
        handle = _make_handle(rootfs, dataset=str(rootfs), mounts=mounts)

        store.destroy(handle)

        assert calls == [
            ("umount", str(rootfs / "mnt" / "nested")),
            ("umount", str(rootfs / "mnt")),
        ]
        # rm -rf actually ran, AFTER the (faked) unmounts above.
        assert not rootfs.exists()


# ---------------------------------------------------------------------------
# 6. End-to-end resolve()+unpack() race for the SAME image_ref, two Store
#    instances (simulating two concurrent `jailrun run` processes), real
#    threads, monkeypatched skopeo/zfs subprocess calls.
# ---------------------------------------------------------------------------


def test_parallel_resolve_unpack_same_image_is_safe():
    """Two Store instances racing resolve()+unpack() for the SAME image_ref must
    both succeed and agree on the same snapshot_id. The per-image lock (keyed by
    image_id in unpack()) must prevent the two `zfs create` calls from ever
    running concurrently, AND unpack()'s existing idempotency check (snapshot
    already exists) must mean the loser's redundant work is skipped rather than
    re-run — so exactly ONE real `zfs create` happens, not two."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        oci_cache = base / "oci"
        mountpoint_base = base / "var"
        image_ref = "example.com/repo:tag"

        call_log: list[tuple] = []
        call_log_guard = threading.Lock()
        create_in_progress = threading.Event()
        overlap_detected = threading.Event()
        create_calls: list[str] = []
        snapshot_created: set[str] = set()
        state_guard = threading.Lock()

        def make_fake_run():
            def fake_run(cmd, cwd=None, input_=None, timeout=None, env=None):  # noqa: ARG001
                with call_log_guard:
                    call_log.append(tuple(cmd))
                if cmd[0] == "skopeo":
                    oci_dest = cmd[-1]  # "oci:<dir>:<tag>"
                    body = oci_dest[len("oci:"):]
                    oci_dir_str, _tag = body.rsplit(":", 1)
                    _seed_fake_oci_layout(Path(oci_dir_str))
                    return None
                if cmd[0] == "zfs" and cmd[1] == "create":
                    if create_in_progress.is_set():
                        overlap_detected.set()
                    create_in_progress.set()
                    time.sleep(0.2)  # simulate slow disk work -> real race window
                    with state_guard:
                        create_calls.append(cmd[-1])
                    create_in_progress.clear()
                    return None
                if cmd[0] == "zfs" and cmd[1] == "snapshot":
                    with state_guard:
                        snapshot_created.add(cmd[2])
                    return None
                if cmd[0] == "zfs" and cmd[1] == "set":
                    return None
                raise AssertionError(f"unexpected cmd in fake_run: {cmd}")
            return fake_run

        def make_fake_run_ok():
            def fake_run_ok(cmd, cwd=None, timeout=None):  # noqa: ARG001
                if cmd[0] == "zfs" and cmd[1] == "list":
                    snap_id = cmd[-1]
                    with state_guard:
                        exists = snap_id in snapshot_created
                    return (0 if exists else 1), b"", b""
                raise AssertionError(f"unexpected cmd in fake_run_ok: {cmd}")
            return fake_run_ok

        store1 = Store(backend="zfs", pool="jailrun", oci_cache_dir=str(oci_cache),
                        mountpoint_base=str(mountpoint_base))
        store2 = Store(backend="zfs", pool="jailrun", oci_cache_dir=str(oci_cache),
                        mountpoint_base=str(mountpoint_base))
        for s in (store1, store2):
            s._run = make_fake_run()
            s._run_ok = make_fake_run_ok()

        results: list[tuple] = []
        results_guard = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(store):
            barrier.wait()
            try:
                image_id = store.resolve(image_ref)
                snapshot_id = store.unpack(image_id)
                with results_guard:
                    results.append(("ok", image_id, snapshot_id))
            except Exception as exc:  # noqa: BLE001
                with results_guard:
                    results.append(("error", repr(exc)))

        threads = [
            threading.Thread(target=worker, args=(store1,)),
            threading.Thread(target=worker, args=(store2,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads), "a worker thread hung"

        assert len(results) == 2, results
        assert all(r[0] == "ok" for r in results), f"a worker failed: {results}"

        image_ids = {r[1] for r in results}
        snapshot_ids = {r[2] for r in results}
        assert len(image_ids) == 1, f"both callers must agree on image_id: {results}"
        assert len(snapshot_ids) == 1, f"both callers must agree on snapshot_id: {results}"

        assert not overlap_detected.is_set(), (
            "two `zfs create` calls for the same image overlapped in time — "
            "the per-image lock did not serialize them"
        )
        assert len(create_calls) == 1, (
            "expected exactly one real `zfs create` (second caller's unpack() "
            f"should have been skipped by the snapshot-exists idempotency check): {create_calls}"
        )


TESTS = [
    test_same_image_lock_serializes_across_threads,
    test_different_image_locks_do_not_serialize,
    test_lock_released_on_exception,
    test_mounts_deepest_first_orders_by_actual_depth_not_insertion_order,
    test_mounts_deepest_first_ties_keep_reverse_insertion_order,
    test_destroy_unmounts_deepest_first_then_zfs_destroy_once,
    test_destroy_plaindir_backend_still_unmounts_before_removing_rootfs,
    test_parallel_resolve_unpack_same_image_is_safe,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/test_base_mountpoint.py
# PURPOSE: regression tests for Store.base_mountpoint()
# INTENT: engine.py needs to resolve a register_base()-returned snapshot_id back to the
#         host directory the base actually lives in, so it can bind-mount it into a
#         per-run clone. Covers both backends.
# DEPENDENCIES: stdlib (tempfile, pathlib, os, sys), store.store (Store)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_base_mountpoint.py — regression tests for Store.base_mountpoint().

Run on host (no FreeBSD/ZFS required — this is pure path computation):
    python3 -m pytest store/test_base_mountpoint.py -v
"""

import os
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from store.store import Store  # noqa: E402


def test_zfs_backend_derives_mountpoint_from_dataset_name():
    s = Store(backend="zfs", pool="jailrun", mountpoint_base="/var/jailrun")
    snapshot_id = "jailrun/bases/esphome-native-abc123def456@snap"
    mp = s.base_mountpoint(snapshot_id)
    assert mp == Path("/var/jailrun/bases/esphome-native-abc123def456")


def test_plaindir_backend_snapshot_id_is_already_the_path():
    s = Store(backend="plaindir", mountpoint_base="/var/jailrun")
    snapshot_id = "/var/jailrun/bases/esphome-native-abc123def456"
    mp = s.base_mountpoint(snapshot_id)
    assert mp == Path(snapshot_id)


TESTS = [
    test_zfs_backend_derives_mountpoint_from_dataset_name,
    test_plaindir_backend_snapshot_id_is_already_the_path,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

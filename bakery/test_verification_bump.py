#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: bakery/test_verification_bump.py
# PURPOSE: unit tests for fill_artifact_paths()'s L0->L1 native.verification bump
#          ("guessed" -> "exists"), pure functions only — no subprocess, no real Store
# INTENT: milestone 0.3's verification ladder (schemas/substitution-manifest.schema.json)
#         says "exists" (L1) means the native artifact path was CONFIRMED present on a
#         real host. bakery(S4) is the only stage allowed to make that specific bump —
#         and only when the resolved path is genuinely there; a resolve_pkg() heuristic
#         guess, or a path that plain isn't there yet, must leave verification at
#         "guessed" rather than silently claiming more confidence than actually earned.
#         Mirrors bakery/test_plan_to_provision_cmd.py's approach: build a
#         ProvisioningPlan directly (no real Store/pkg/zfs needed), so this runs on
#         plain Linux CI.
# DEPENDENCIES: stdlib (os, sys, tempfile, pathlib), bakery.bakery (fill_artifact_paths,
#               ProvisioningPlan)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_verification_bump.py — pure unit tests for bakery's native.verification bump.

Run on host (no FreeBSD required):
    python3 -m pytest bakery/test_verification_bump.py -v
"""

import os
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bakery.bakery import ProvisioningPlan, fill_artifact_paths  # noqa: E402


def _manifest_with_native(provider: str, verification: str = "guessed") -> dict:
    return {
        "image": "test:latest",
        "binaries": [
            {
                "path": "/usr/bin/cmake",
                "status": "native",
                "native": {"provider": provider, "artifact_path": None, "verification": verification},
            },
        ],
        "linuxulator": {"required": False},
    }


# CONTRACT: a resolved artifact_path that REALLY EXISTS on disk bumps guessed -> exists
def test_bump_guessed_to_exists_when_artifact_confirmed_present():
    """
    A real file (created via tempfile, so this works the same on Linux CI as it
    would on a real FreeBSD base) at the resolved artifact_path -> verification
    is bumped from 'guessed' to 'exists'.
    """
    fd, real_path = tempfile.mkstemp(prefix="jailrun-cmake-")
    os.close(fd)
    try:
        manifest = _manifest_with_native("pkg:cmake", verification="guessed")
        plan = ProvisioningPlan()
        plan.add_pkg("cmake", real_path)

        updated = fill_artifact_paths(manifest, plan)

        native = updated["binaries"][0]["native"]
        assert native["artifact_path"] == real_path
        assert native["verification"] == "exists", (
            f"expected verification bumped to 'exists', got {native.get('verification')!r}"
        )
        # original manifest must not be mutated (fill_artifact_paths deep-copies)
        assert manifest["binaries"][0]["native"]["verification"] == "guessed"
    finally:
        os.unlink(real_path)


# CONTRACT: a resolved artifact_path that does NOT exist on disk leaves verification="guessed"
#           (covers resolve_pkg's heuristic "guessing ..." fallback — a path we've never confirmed)
def test_guessed_stays_guessed_when_artifact_path_does_not_exist():
    """A resolved path with nothing there (e.g. an unconfirmed resolve_pkg guess) stays 'guessed'."""
    with tempfile.TemporaryDirectory() as td:
        nonexistent_path = str(Path(td) / "no-such-binary")
        manifest = _manifest_with_native("pkg:cmake", verification="guessed")
        plan = ProvisioningPlan()
        plan.add_pkg("cmake", nonexistent_path)

        updated = fill_artifact_paths(manifest, plan)

        native = updated["binaries"][0]["native"]
        assert native["artifact_path"] == nonexistent_path
        assert native["verification"] == "guessed", (
            f"expected verification to stay 'guessed', got {native.get('verification')!r}"
        )


# CONTRACT: a provider with no matching resolved plan step ("artifact is absent") leaves
#           native.artifact_path/verification untouched entirely
def test_guessed_stays_guessed_when_provider_unresolved():
    """No plan step at all for this provider -> fill_artifact_paths warns and skips it untouched."""
    manifest = _manifest_with_native("pkg:cmake", verification="guessed")
    plan = ProvisioningPlan()  # empty — nothing resolved for pkg:cmake

    updated = fill_artifact_paths(manifest, plan)

    native = updated["binaries"][0]["native"]
    assert native["artifact_path"] is None, "unresolved provider must not get an artifact_path"
    assert native["verification"] == "guessed"


TESTS = [
    test_bump_guessed_to_exists_when_artifact_confirmed_present,
    test_guessed_stays_guessed_when_artifact_path_does_not_exist,
    test_guessed_stays_guessed_when_provider_unresolved,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: providers/test_flavor_policy.py
# PURPOSE: prove providers/coreutils-flavor.json (the GNU-vs-BSD flavor labels for
#          coreutils-class binaries, see docs/GNU-VS-BSD-POLICY.md) is well-formed,
#          points only at real provider-map.json entries, is exposed correctly by the
#          providers/ loader as COREUTILS_FLAVOR, and — most importantly — that adding
#          it did NOT touch provider-map.json's existing binary->provider mappings.
# INTENT: 2026-07-22 — ROADMAP.md 0.3's "Substitution policy per entry" calls for a
#         conservative GNU-flavored default for coreutils-class binaries. This is the
#         first, purely-additive step: label which flavor each EXISTING provider-map.json
#         entry for a coreutils-class binary already resolves to, in a brand-new,
#         separate data file — never by changing provider-map.json's own value shape or
#         PROVIDER_MAP's dict[str, str] contract (probe.py relies on that contract).
#         test_provider_map_unchanged_by_flavor_work below is the load-bearing proof of
#         that constraint: it round-trips PROVIDER_MAP against the exact same fixture
#         providers/test_registry_roundtrip.py already uses to pin the 55 pre-existing
#         entries, so any accidental edit to provider-map.json's real data fails here.
# DEPENDENCIES: stdlib (json, os, sys, pathlib), jsonschema, providers
#               (COREUTILS_FLAVOR, PROVIDER_MAP), providers.test_registry_roundtrip
#               (EXPECTED_PROVIDER_MAP fixture, reused rather than re-pasted)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_flavor_policy.py — tests for providers/coreutils-flavor.json and the
COREUTILS_FLAVOR constant it feeds providers/__init__.py.

Run on host (no FreeBSD required):
    python3 -m pytest providers/test_flavor_policy.py -v
    # or directly:
    python3 providers/test_flavor_policy.py
"""

import json
import os
import sys
from pathlib import Path

import jsonschema

_ROOT = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_ROOT)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from providers import COREUTILS_FLAVOR, PROVIDER_MAP  # noqa: E402
from providers.test_registry_roundtrip import EXPECTED_PROVIDER_MAP  # noqa: E402

_PROVIDERS_DIR = Path(__file__).parent


# CONTRACT: providers/coreutils-flavor.json validates against the coreutils_flavor
#           $defs entry registry.schema.json defines for it.
def test_coreutils_flavor_validates_against_schema():
    schema = json.loads((_PROVIDERS_DIR / "registry.schema.json").read_text())
    data = json.loads((_PROVIDERS_DIR / "coreutils-flavor.json").read_text())
    jsonschema.validate(data, schema["$defs"]["coreutils_flavor"])


# CONTRACT: coreutils-flavor.json only LABELS binaries that already exist as keys in
#           provider-map.json — it must never point at a binary provider-map.json
#           doesn't know about (that would be inventing a mapping, out of scope here).
def test_no_orphan_keys_in_coreutils_flavor():
    assert len(COREUTILS_FLAVOR) > 0, "COREUTILS_FLAVOR should not be empty"
    for binary in COREUTILS_FLAVOR:
        assert binary in PROVIDER_MAP, (
            f"coreutils-flavor.json has key {binary!r} which is not a key in "
            "provider-map.json's providers map"
        )


# CONTRACT: every flavor value is one of the two allowed labels (belt-and-suspenders on
#           top of the schema check above — this is what a Python consumer would assert).
def test_flavor_values_are_gnu_or_bsd():
    for binary, flavor in COREUTILS_FLAVOR.items():
        assert flavor in ("gnu", "bsd"), f"{binary!r} has unexpected flavor {flavor!r}"


# CONTRACT: the loader exposes COREUTILS_FLAVOR as a plain dict[str, str], loaded from
#           providers/coreutils-flavor.json's "flavors" object — same loading style
#           providers/__init__.py already uses for PROVIDER_MAP et al.
def test_loader_exposes_coreutils_flavor_correctly():
    assert isinstance(COREUTILS_FLAVOR, dict)
    raw = json.loads((_PROVIDERS_DIR / "coreutils-flavor.json").read_text())
    assert COREUTILS_FLAVOR == raw["flavors"]
    # "tar" -> pkg:gtar in provider-map.json is the one entry this task actually found
    # and classified (see docs/GNU-VS-BSD-POLICY.md) — pin it explicitly so a future
    # accidental edit of coreutils-flavor.json is caught, not just a shape change.
    assert COREUTILS_FLAVOR.get("tar") == "gnu"


# CONTRACT (the most important one): adding coreutils-flavor.json, its schema entry,
# and its loader constant must NOT have changed provider-map.json's existing
# binary->provider data, PROVIDER_MAP's type, or its contents in any way. This is the
# round-trip proof, reusing the SAME fixture providers/test_registry_roundtrip.py pins
# (EXPECTED_PROVIDER_MAP, pasted verbatim from the pre-extraction probe.py literal) —
# not a re-derived or trimmed copy — so this test can only pass if PROVIDER_MAP is
# byte-for-byte identical to what it was before this task's changes.
def test_provider_map_unchanged_by_flavor_work():
    assert isinstance(PROVIDER_MAP, dict)
    assert len(PROVIDER_MAP) == 55 == len(EXPECTED_PROVIDER_MAP)
    assert PROVIDER_MAP == EXPECTED_PROVIDER_MAP


TESTS = [
    test_coreutils_flavor_validates_against_schema,
    test_no_orphan_keys_in_coreutils_flavor,
    test_flavor_values_are_gnu_or_bsd,
    test_loader_exposes_coreutils_flavor_correctly,
    test_provider_map_unchanged_by_flavor_work,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

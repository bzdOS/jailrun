#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: .github/scripts/validate_provider_data.py
# PURPOSE: CI check — providers/*.json data files (PROVIDER_MAP, PKG_ARTIFACTS,
#          PORT_ARTIFACTS, MULTI_BINARY_PKGS, COREUTILS_FLAVOR) must validate against
#          providers/registry.schema.json, and the providers/ loader must expose
#          them as the same objects probe.py/bakery.py consume, on every push.
# INTENT: sibling to validate_manifest_schema.py's approach (same shape: build/load
#         real inputs, validate against the real schema, exit 1 on any failure) —
#         roadmap 0.3 moved these lookup tables from hard-coded Python dict/set
#         literals to schema-validated JSON under providers/ so the registry can grow
#         as data (agent-contributed) rather than as code edits. This script is the
#         CI guard against a malformed or drifted data file slipping in.
# DEPENDENCIES: stdlib (json, sys, os, pathlib), jsonschema, providers
# PUBLIC_API: none — script, exits 1 on any validation failure
# END_AI_HEADER
"""
validate_provider_data.py — validate providers/*.json against
providers/registry.schema.json, and sanity-check that providers/__init__.py's
loaded objects are non-empty and of the expected type.

Each data file is validated against the $defs entry in registry.schema.json that
matches its own shape (see FILE_TO_SCHEMA_DEF below) — not the schema's top-level
`anyOf` (which would only prove the file matches AT LEAST ONE known shape, not
the SPECIFIC one for its filename).
"""

import json
import os
import sys
from pathlib import Path

import jsonschema

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_PROVIDERS_DIR = Path(_ROOT, "providers")

# Maps each data file to the $defs key in registry.schema.json that describes its shape.
# verification.json is GENERATED (by providers/backfill.py on a FreeBSD host), so it
# is validated only WHEN PRESENT — a clean tree without a freshly-run backfill must
# still pass CI. The other four are hand-maintained and always required.
FILE_TO_SCHEMA_DEF = {
    "provider-map.json": "provider_map",
    "pkg-artifacts.json": "pkg_artifacts",
    "port-artifacts.json": "port_artifacts",
    "multi-binary-pkgs.json": "multi_binary_pkgs",
    "multi-binary-suffix.json": "multi_binary_suffix",
    "coreutils-flavor.json": "coreutils_flavor",
    "verification.json": "verification",
}


def main() -> None:
    registry_schema = json.loads((_PROVIDERS_DIR / "registry.schema.json").read_text())
    defs = registry_schema["$defs"]

    # START_VALIDATE_EACH_DATA_FILE
    for filename, def_name in FILE_TO_SCHEMA_DEF.items():
        data_path = _PROVIDERS_DIR / filename
        if filename == "verification.json" and not data_path.exists():
            # GENERATED file — absent is fine on a tree that hasn't run a backfill.
            print(f"skip: providers/{filename} not generated yet (run `python3 -m providers.backfill` on FreeBSD)")
            continue
        data = json.loads(data_path.read_text())
        jsonschema.validate(data, defs[def_name])
        print(f"OK: providers/{filename} validates against $defs/{def_name}")
    # END_VALIDATE_EACH_DATA_FILE

    # START_SANITY_CHECK_LOADER
    # Import AFTER schema validation so a malformed JSON file fails with a clear
    # jsonschema error above, rather than an opaque loader traceback here.
    from providers import (
        COREUTILS_FLAVOR,
        MULTI_BINARY_PKGS,
        MULTI_BINARY_SUFFIX,
        PKG_ARTIFACTS,
        PORT_ARTIFACTS,
        PROVIDER_MAP,
    )

    assert isinstance(PROVIDER_MAP, dict) and len(PROVIDER_MAP) > 0, "PROVIDER_MAP is empty or wrong type"
    assert isinstance(PKG_ARTIFACTS, dict) and len(PKG_ARTIFACTS) > 0, "PKG_ARTIFACTS is empty or wrong type"
    assert isinstance(PORT_ARTIFACTS, dict) and len(PORT_ARTIFACTS) > 0, "PORT_ARTIFACTS is empty or wrong type"
    assert isinstance(MULTI_BINARY_PKGS, frozenset), "MULTI_BINARY_PKGS must be a frozenset"
    assert isinstance(MULTI_BINARY_SUFFIX, dict), "MULTI_BINARY_SUFFIX must be a dict"
    # Every suffix key must be an already-declared multi-binary pkg (suffix only
    # refines an existing multi-binary pkg, never declares a new one).
    _suffix_orphans = [k for k in MULTI_BINARY_SUFFIX if k not in MULTI_BINARY_PKGS]
    assert not _suffix_orphans, f"MULTI_BINARY_SUFFIX keys not in MULTI_BINARY_PKGS: {_suffix_orphans}"
    assert isinstance(COREUTILS_FLAVOR, dict) and len(COREUTILS_FLAVOR) > 0, "COREUTILS_FLAVOR is empty or wrong type"
    # Every coreutils-flavor.json key must point at a binary that actually exists in
    # provider-map.json — this file only LABELS existing mappings, it never invents new ones.
    orphans = [k for k in COREUTILS_FLAVOR if k not in PROVIDER_MAP]
    assert not orphans, f"COREUTILS_FLAVOR has keys not present in PROVIDER_MAP: {orphans}"
    # verification.json (when present) is keyed by the same basenames as provider-map.json
    # and its per-entry provider must match — a drift here means the backfill and the
    # shipped registry disagree, which is exactly what this CI guard exists to catch.
    _verification_path = _PROVIDERS_DIR / "verification.json"
    if _verification_path.exists():
        _verification = json.loads(_verification_path.read_text())
        _v_entries = _verification.get("entries", {})
        _v_orphans = [k for k in _v_entries if k not in PROVIDER_MAP]
        assert not _v_orphans, f"verification.json has keys not in PROVIDER_MAP: {_v_orphans}"
        _v_drift = [
            k for k, v in _v_entries.items()
            if v.get("provider") != PROVIDER_MAP[k]
        ]
        assert not _v_drift, (
            "verification.json provider strings diverge from provider-map.json: "
            + ", ".join(f"{k}={_v_entries[k]['provider']}!={PROVIDER_MAP[k]}" for k in _v_drift)
        )
        print(f"OK: providers/verification.json consistent with PROVIDER_MAP ({len(_v_entries)} entries)")
    print(
        "OK: providers loader exposes non-empty PROVIDER_MAP "
        f"({len(PROVIDER_MAP)}), PKG_ARTIFACTS ({len(PKG_ARTIFACTS)}), "
        f"PORT_ARTIFACTS ({len(PORT_ARTIFACTS)}), MULTI_BINARY_PKGS ({len(MULTI_BINARY_PKGS)}), "
        f"MULTI_BINARY_SUFFIX ({len(MULTI_BINARY_SUFFIX)}), "
        f"COREUTILS_FLAVOR ({len(COREUTILS_FLAVOR)})"
    )
    # END_SANITY_CHECK_LOADER

    # START_SANITY_CHECK_REEXPORT
    # probe.py / bakery.py must re-export the SAME objects, not copies.
    from probe.probe import PROVIDER_MAP as probe_provider_map
    from bakery.bakery import PKG_ARTIFACTS as bakery_pkg_artifacts

    assert probe_provider_map is PROVIDER_MAP, "probe.probe.PROVIDER_MAP is not providers.PROVIDER_MAP"
    assert bakery_pkg_artifacts is PKG_ARTIFACTS, "bakery.bakery.PKG_ARTIFACTS is not providers.PKG_ARTIFACTS"
    print("OK: probe.probe / bakery.bakery re-export the same providers objects")
    # END_SANITY_CHECK_REEXPORT


if __name__ == "__main__":
    main()

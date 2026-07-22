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
FILE_TO_SCHEMA_DEF = {
    "provider-map.json": "provider_map",
    "pkg-artifacts.json": "pkg_artifacts",
    "port-artifacts.json": "port_artifacts",
    "multi-binary-pkgs.json": "multi_binary_pkgs",
    "coreutils-flavor.json": "coreutils_flavor",
}


def main() -> None:
    registry_schema = json.loads((_PROVIDERS_DIR / "registry.schema.json").read_text())
    defs = registry_schema["$defs"]

    # START_VALIDATE_EACH_DATA_FILE
    for filename, def_name in FILE_TO_SCHEMA_DEF.items():
        data_path = _PROVIDERS_DIR / filename
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
        PKG_ARTIFACTS,
        PORT_ARTIFACTS,
        PROVIDER_MAP,
    )

    assert isinstance(PROVIDER_MAP, dict) and len(PROVIDER_MAP) > 0, "PROVIDER_MAP is empty or wrong type"
    assert isinstance(PKG_ARTIFACTS, dict) and len(PKG_ARTIFACTS) > 0, "PKG_ARTIFACTS is empty or wrong type"
    assert isinstance(PORT_ARTIFACTS, dict) and len(PORT_ARTIFACTS) > 0, "PORT_ARTIFACTS is empty or wrong type"
    assert isinstance(MULTI_BINARY_PKGS, frozenset), "MULTI_BINARY_PKGS must be a frozenset"
    assert isinstance(COREUTILS_FLAVOR, dict) and len(COREUTILS_FLAVOR) > 0, "COREUTILS_FLAVOR is empty or wrong type"
    # Every coreutils-flavor.json key must point at a binary that actually exists in
    # provider-map.json — this file only LABELS existing mappings, it never invents new ones.
    orphans = [k for k in COREUTILS_FLAVOR if k not in PROVIDER_MAP]
    assert not orphans, f"COREUTILS_FLAVOR has keys not present in PROVIDER_MAP: {orphans}"
    print(
        "OK: providers loader exposes non-empty PROVIDER_MAP "
        f"({len(PROVIDER_MAP)}), PKG_ARTIFACTS ({len(PKG_ARTIFACTS)}), "
        f"PORT_ARTIFACTS ({len(PORT_ARTIFACTS)}), MULTI_BINARY_PKGS ({len(MULTI_BINARY_PKGS)}), "
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

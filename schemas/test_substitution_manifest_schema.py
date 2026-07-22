#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: schemas/test_substitution_manifest_schema.py
# PURPOSE: unit tests pinning schemas/substitution-manifest.schema.json's native.verification
#          enum (the L0-L4 trust ladder added for roadmap milestone 0.3)
# INTENT: the verification field must (a) accept all five documented levels
#         ("guessed"/"exists"/"runs"/"behaves"/"proven"), (b) stay OPTIONAL so
#         absent still means "unknown/weakest", (c) reject unknown enum values,
#         and (d) real probe(S2) output (which now stamps verification="guessed"
#         on every proposed native block) must keep validating end to end —
#         mirrors .github/scripts/validate_manifest_schema.py's approach of
#         running the REAL probe() against a tiny synthetic rootfs rather than
#         a hand-built dict, so drift between probe.py and the schema is caught.
# DEPENDENCIES: stdlib (json, struct, sys, tempfile, pathlib), jsonschema, probe (probe)
# PUBLIC_API: run_all, TESTS list; each test_* function is also callable directly by pytest
# END_AI_HEADER
"""
test_substitution_manifest_schema.py — schema-level tests for native.verification.

Run on host (Linux/linux-host) — no FreeBSD required:
    python3 -m pytest schemas/test_substitution_manifest_schema.py -v
    # or directly:
    python3 schemas/test_substitution_manifest_schema.py
"""

import json
import struct
import sys
import tempfile
from pathlib import Path

import jsonschema

# Insert probe/ itself (not the repo root) onto sys.path, matching
# probe/test_classify.py and probe/test_verification.py's own convention —
# this makes "probe" resolve to the flat probe/probe.py module. Using the
# repo-root + "from probe.probe import probe" package-style import instead
# would collide under pytest: whichever test module runs first pins
# sys.modules["probe"] to one shape or the other, breaking the second style
# to import in the same process.
_PROBE_DIR = Path(__file__).parent.parent / "probe"
if str(_PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBE_DIR))

from probe import probe as run_probe  # noqa: E402

_SCHEMA_PATH = Path(__file__).parent / "substitution-manifest.schema.json"

VERIFICATION_LEVELS = ["guessed", "exists", "runs", "behaves", "proven"]


# load_schema:start
#   purpose: parse the substitution manifest JSON schema from disk
#   input: none
#   output: schema: dict — parsed JSON Schema document
#   sideEffects: reads schemas/substitution-manifest.schema.json from disk
def load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())
# load_schema:end


# _minimal_manifest:start
#   purpose: build the smallest manifest dict that satisfies the schema's top-level
#            "required" fields, with one binary entry carrying a caller-supplied native block
#   input:
#     native: dict | None — native block to attach to the single binary entry (or None to omit it)
#   output:
#     manifest: dict — minimal valid-shape Substitution Manifest
#   sideEffects: none
def _minimal_manifest(native: dict | None) -> dict:
    binary: dict = {
        "path": "/usr/bin/cmake",
        "abi": "linux",
        "status": "native",
    }
    if native is not None:
        binary["native"] = native
    return {
        "image": "schema-test:latest",
        "binaries": [binary],
        "linuxulator": {"required": False},
    }
# _minimal_manifest:end


# ---------------------------------------------------------------------------
# Tests — native.verification enum shape
# ---------------------------------------------------------------------------

# CONTRACT: each of the five documented verification levels validates against the schema
def test_each_verification_level_validates():
    """guessed/exists/runs/behaves/proven all validate inside native.verification."""
    schema = load_schema()
    for level in VERIFICATION_LEVELS:
        manifest = _minimal_manifest({"provider": "pkg:cmake", "artifact_path": None, "verification": level})
        jsonschema.validate(manifest, schema)  # raises on failure
    print("PASS test_each_verification_level_validates")


# CONTRACT: native.verification is OPTIONAL — a native block without it still validates
#           (absent = unknown/weakest, never required to be populated)
def test_verification_field_is_optional():
    """A native block with no verification key at all still validates."""
    schema = load_schema()
    manifest = _minimal_manifest({"provider": "pkg:cmake", "artifact_path": None})
    jsonschema.validate(manifest, schema)
    print("PASS test_verification_field_is_optional")


# CONTRACT: an unrecognised verification string is rejected by the schema
def test_unknown_verification_value_is_rejected():
    """A made-up verification level must fail jsonschema validation."""
    schema = load_schema()
    manifest = _minimal_manifest(
        {"provider": "pkg:cmake", "artifact_path": None, "verification": "trust-me-bro"}
    )
    try:
        jsonschema.validate(manifest, schema)
        raise AssertionError("expected ValidationError for an unknown verification value")
    except jsonschema.ValidationError:
        pass
    print("PASS test_unknown_verification_value_is_rejected")


# ---------------------------------------------------------------------------
# Test — real probe() output (verification="guessed") validates end to end
# ---------------------------------------------------------------------------

# write_fake_elf:start
#   purpose: materialise a minimal syntactically valid Linux ELF header at `path`
#   input:
#     path: Path — destination file path (parent dirs must already exist)
#   output: none
#   sideEffects: writes a 64-byte file at `path` and chmods it 0o755 (executable)
def write_fake_elf(path: Path) -> None:
    e_ident = bytearray(16)
    e_ident[0:4] = b"\x7fELF"
    e_ident[4] = 2       # ELFCLASS64
    e_ident[5] = 1       # little-endian
    e_ident[6] = 1       # EI_VERSION
    e_ident[7] = 0       # ELFOSABI_NONE -> classified 'linux' by convention
    header = bytes(e_ident) + struct.pack("<HH", 2, 62)  # ET_EXEC, EM_X86_64
    header += b"\x00" * (64 - len(header))
    path.write_bytes(header)
    path.chmod(0o755)
# write_fake_elf:end


# CONTRACT: real probe() output — which now stamps native.verification="guessed" on every
#           proposed native block — validates against the schema end to end
def test_real_probe_output_with_guessed_verification_validates():
    """
    Mirrors .github/scripts/validate_manifest_schema.py: run the REAL probe()
    against a tiny synthetic rootfs (not a hand-built dict) so drift between
    probe.py's native.verification stamping and the schema is caught.
    """
    schema = load_schema()
    with tempfile.TemporaryDirectory() as td:
        rootfs = Path(td) / "rootfs"
        (rootfs / "usr" / "bin").mkdir(parents=True)
        write_fake_elf(rootfs / "usr" / "bin" / "cmake")  # known provider -> native block proposed

        manifest = run_probe(str(rootfs), image_ref="schema-test:latest")
        jsonschema.validate(manifest, schema)

        entries = [b for b in manifest["binaries"] if b["path"] == "/usr/bin/cmake"]
        assert len(entries) == 1
        assert entries[0]["native"]["verification"] == "guessed"
    print("PASS test_real_probe_output_with_guessed_verification_validates")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_each_verification_level_validates,
    test_verification_field_is_optional,
    test_unknown_verification_value_is_rejected,
    test_real_probe_output_with_guessed_verification_validates,
]


# run_all:start
#   purpose: execute every function in TESTS, collect failures, report pass/fail counts
#   input: none
#   output: none (results printed to stdout)
#   sideEffects: prints PASS/FAIL/ERROR lines per test; calls sys.exit(1) if any failure
def run_all():
    failures = []
    for fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            print(f"FAIL {fn.__name__}: {exc}")
            failures.append(fn.__name__)
        except Exception as exc:
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            failures.append(fn.__name__)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"All {len(TESTS)} tests passed.")
# run_all:end


if __name__ == "__main__":
    run_all()

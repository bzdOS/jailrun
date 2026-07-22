#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_explain.py
# PURPOSE: unit tests for render_explain() — pure formatter over a synthetic
#          substitution manifest dict, no VM, no real image, runnable on Linux
# INTENT: covers the WHY-distinction logic (no native block vs. unresolved
#         artifact_path), the per-binary table contents (including verification
#         level surfacing), the summary counts, and both text/json fmt paths
# DEPENDENCIES: stdlib (json), runtime.explain (render_explain)
# PUBLIC_API: each test_* function is callable directly by pytest
# END_AI_HEADER
"""
test_explain.py — pure unit tests for runtime/explain.py's render_explain().

Run on host (no FreeBSD required):
    python3 -m pytest runtime/test_explain.py -v
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from runtime.explain import render_explain  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic manifest fixture
# ---------------------------------------------------------------------------

# _manifest:start
#   purpose: build a synthetic substitution manifest covering the distinct
#            binary shapes render_explain() must handle
#   input: none
#   output:
#     manifest: dict — one binary of each kind:
#       - python3: native, provider pkg:python311, verification "runs"
#       - gcc: linuxulator, no native block at all (WHY = no mapping)
#       - openssl: linuxulator, native block present but artifact_path null,
#         provider "pkg:openssl" (WHY = proposed but not resolved)
#       - entrypoint.sh: script, native (nothing to substitute)
#     plus linuxulator.required = True (gcc/openssl are still linuxulator)
#   sideEffects: none
def _manifest() -> dict:
    return {
        "image": "esphome/esphome:2025.5",
        "binaries": [
            {
                "path": "/usr/bin/python3",
                "role": "load-bearing",
                "abi": "linux",
                "status": "native",
                "native": {
                    "provider": "pkg:python311",
                    "artifact_path": "/usr/local/bin/python3.11",
                    "verification": "runs",
                },
                "syscalls_needed": [],
                "notes": "",
            },
            {
                "path": "/usr/bin/gcc",
                "role": "auxiliary",
                "abi": "linux",
                "status": "linuxulator",
                "syscalls_needed": [],
                "notes": "no provider mapped yet",
            },
            {
                "path": "/usr/bin/openssl",
                "role": "auxiliary",
                "abi": "linux",
                "status": "linuxulator",
                "native": {
                    "provider": "pkg:openssl",
                    "artifact_path": None,
                },
                "syscalls_needed": [],
                "notes": "provider proposed, bakery hasn't resolved it",
            },
            {
                "path": "/entrypoint.sh",
                "role": "load-bearing",
                "abi": "script",
                "status": "native",
                "syscalls_needed": [],
                "notes": "",
            },
        ],
        "linuxulator": {
            "required": True,
            "gaps": [],
            "risk": "low",
        },
    }
# _manifest:end


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# CONTRACT: native binary with verification="runs" -> text output surfaces basename, status, provider, and verification level
def test_native_verified_binary_appears_in_text_table():
    out = render_explain(_manifest(), fmt="text")
    assert "python3" in out
    assert "native" in out
    assert "pkg:python311" in out
    assert "runs" in out, "verification level 'runs' must appear in text output"


# CONTRACT: linuxulator binary with no native block at all -> WHY = "no native provider mapped"
def test_linuxulator_no_native_block_why_is_no_mapping():
    out = render_explain(_manifest(), fmt="text")
    assert "gcc: no native provider mapped" in out


# CONTRACT: linuxulator binary with a native block but null artifact_path -> WHY = "provider proposed but not resolved"
def test_linuxulator_unresolved_artifact_why_is_proposed_not_resolved():
    out = render_explain(_manifest(), fmt="text")
    assert "openssl: provider proposed but not resolved" in out


# CONTRACT: linuxulator binary whose native.provider is "pkg:NAME" -> hint surfaces exact "pkg install NAME"
def test_hint_surfaces_pkg_install_for_pkg_provider():
    out = render_explain(_manifest(), fmt="text")
    assert "pkg install openssl" in out


# CONTRACT: linuxulator binary with no provider at all -> generic "add a pkg:/port: mapping for <basename>" hint
def test_hint_is_generic_mapping_suggestion_when_no_provider():
    out = render_explain(_manifest(), fmt="text")
    assert "add a pkg:/port: mapping for gcc" in out


# CONTRACT: abi=="script" status=="native" binary (entrypoint.sh) appears in the table without a WHY line
def test_script_binary_appears_without_why():
    out = render_explain(_manifest(), fmt="text")
    assert "entrypoint.sh" in out
    assert "WHY" in out, "fixture has linuxulator entries, so a WHY section must be present"
    why_section = out.split("WHY", 1)[1]
    assert "entrypoint.sh:" not in why_section


# CONTRACT: summary line reports "N/total native" (2 of 4 here: python3 + entrypoint.sh) and linuxulator required
def test_summary_counts_and_linuxulator_required():
    out = render_explain(_manifest(), fmt="text")
    assert "2/4 native" in out
    assert "Linuxulator required: yes" in out


# CONTRACT: fmt="json" parses as valid JSON with correct counts/linuxulator_required/binaries length
def test_json_format_parses_with_correct_counts():
    out = render_explain(_manifest(), fmt="json")
    data = json.loads(out)
    assert data["counts"] == {"native": 2, "linuxulator": 2, "total": 4}
    assert data["linuxulator_required"] is True
    assert len(data["binaries"]) == 4


# CONTRACT: json binaries[] entries carry path/abi/status/provider/verification/why, matching the text-mode WHY values
def test_json_binaries_carry_why_and_verification():
    out = render_explain(_manifest(), fmt="json")
    data = json.loads(out)
    by_path = {b["path"]: b for b in data["binaries"]}

    python3 = by_path["/usr/bin/python3"]
    assert python3["status"] == "native"
    assert python3["provider"] == "pkg:python311"
    assert python3["verification"] == "runs"
    assert python3["why"] is None

    gcc = by_path["/usr/bin/gcc"]
    assert gcc["why"] == "no native provider mapped"
    assert gcc["provider"] is None

    openssl = by_path["/usr/bin/openssl"]
    assert openssl["why"] == "provider proposed but not resolved"
    assert openssl["provider"] == "pkg:openssl"


# CONTRACT: a manifest with zero binaries still renders (no crash) with 0/0 native summary
def test_empty_binaries_list_renders_zero_summary():
    manifest = {"image": "scratch:latest", "binaries": [], "linuxulator": {"required": False}}
    out = render_explain(manifest, fmt="text")
    assert "0/0 native" in out
    assert "Linuxulator required: no" in out

    data = json.loads(render_explain(manifest, fmt="json"))
    assert data["counts"] == {"native": 0, "linuxulator": 0, "total": 0}
    assert data["binaries"] == []


if __name__ == "__main__":
    import pytest  # noqa: PLC0415

    sys.exit(pytest.main([__file__, "-v"]))

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/explain.py
# PURPOSE: pure formatter for the substitution manifest — answers "will this image
#          run under jailrun, how, and what would make it better" without the user
#          reading probe/bakery source
# INTENT: ROADMAP.md feature #1 ("jailrun explain <image>"). Cheap by design — this
#          module does no I/O and no subprocess; it only renders a manifest dict
#          that engine.py's probe/bakery pipeline already produces. cli.py owns
#          getting the manifest (from --manifest FILE or from engine's
#          resolve/unpack/clone/_load_manifest path); this module only formats it.
# DEPENDENCIES: stdlib only (json, os.path); schemas/substitution-manifest.schema.json
#               documents the shape of the manifest dict consumed here
# PUBLIC_API: render_explain(manifest, fmt="text") -> str
# END_AI_HEADER

# START_INVARIANTS
# - render_explain() performs no I/O, no subprocess, no imports of engine/store/
#   probe/bakery — it is a pure function over the manifest dict already in hand,
#   so it is fully unit-testable on Linux with a synthetic dict.
# - Every binaries[] entry with status=="linuxulator" gets a WHY, distinguishing
#   "no native provider mapped" (no native block) from "provider proposed but not
#   resolved" (native block present but artifact_path is null/absent) — this is
#   the single most useful line in the whole report, since it tells the operator
#   exactly which gap to close and how.
# END_INVARIANTS

"""
runtime/explain.py — pure rendering logic for `jailrun explain`.

Takes the same substitution manifest dict that engine.py's run() path consumes
(see schemas/substitution-manifest.schema.json) and renders either a
human-readable text report or a machine-readable JSON summary. No I/O here —
callers (runtime/cli.py) are responsible for obtaining the manifest, either by
reading a --manifest FILE from disk or by running the same
resolve → unpack → clone → _load_manifest sequence engine.run() uses.
"""

from __future__ import annotations

import json
import os


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# _basename:start
#   purpose: extract the display basename of a binary's in-image path
#   input:
#     path: str — absolute path inside the image rootfs, e.g. "/usr/bin/python3"
#   output:
#     name: str — basename, e.g. "python3"; falls back to the raw path if empty
#   sideEffects: none
def _basename(path: str) -> str:
    name = os.path.basename(path or "")
    return name or (path or "<unknown>")
# _basename:end


# _why_linuxulator:start
#   purpose: explain why a single binary stayed status=="linuxulator" instead of
#            getting a native substitute
#   input:
#     entry: dict — one binaries[] element from the substitution manifest
#   output:
#     why: str | None — None if entry.status != "linuxulator" (no explanation
#          needed); otherwise one of:
#            "no native provider mapped"            — no native block at all
#            "provider proposed but not resolved"    — native block present but
#                                                       artifact_path is null/absent
#            None                                    — native block AND
#                                                       artifact_path both present
#                                                       (unexpected for this status,
#                                                       but not our place to guess)
#   sideEffects: none
def _why_linuxulator(entry: dict) -> str | None:
    if entry.get("status") != "linuxulator":
        return None
    native = entry.get("native")
    if not native:
        return "no native provider mapped"
    if not native.get("artifact_path"):
        return "provider proposed but not resolved"
    return None
# _why_linuxulator:end


# _hint_for:start
#   purpose: propose the concrete next action that would flip a linuxulator
#            binary to native
#   input:
#     row: dict — a _row()-shaped dict (basename, provider); only meaningful
#          when row["status"] == "linuxulator" (caller filters)
#   output:
#     hint: str — human-actionable suggestion; always non-empty for a
#           linuxulator row
#   sideEffects: none
def _hint_for(row: dict) -> str:
    basename = row["basename"]
    provider = row["provider"]

    if not provider:
        return f"add a pkg:/port: mapping for {basename}"
    if provider.startswith("pkg:"):
        pkg_name = provider[len("pkg:"):]
        return f"pkg install {pkg_name}    # resolves {basename}"
    # port: / build: / anything else proposed-but-unresolved.
    return f"resolve {provider} for {basename} (bakery has not produced an artifact yet)"
# _hint_for:end


# _row:start
#   purpose: build the normalized per-binary summary dict shared by both the
#            text table and the json output
#   input:
#     entry: dict — one binaries[] element from the substitution manifest
#   output:
#     row: dict — {path, basename, abi, status, provider, verification, why}
#          provider/verification are None when absent; why is None unless
#          status == "linuxulator"
#   sideEffects: none
def _row(entry: dict) -> dict:
    native = entry.get("native") or {}
    return {
        "path": entry.get("path", ""),
        "basename": _basename(entry.get("path", "")),
        "abi": entry.get("abi", "unknown"),
        "status": entry.get("status", "unknown"),
        "provider": native.get("provider"),
        "verification": native.get("verification"),
        "why": _why_linuxulator(entry),
    }
# _row:end


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

# _render_text:start
#   purpose: render the human-readable report: per-binary table, WHY section,
#            summary line, and actionable hints
#   input:
#     manifest: dict — full substitution manifest (see schema)
#     rows: list[dict] — pre-computed per-binary rows (see _row)
#   output:
#     report: str — multi-line text report, no trailing newline requirement
#             beyond join("\n")
#   sideEffects: none
def _render_text(manifest: dict, rows: list[dict]) -> str:
    lines: list[str] = []

    image = manifest.get("image") or "<manifest file>"
    lines.append(f"jailrun explain: {image}")
    lines.append("=" * max(len(lines[0]), 40))
    lines.append("")

    # START_TABLE
    if rows:
        col_bin = max(6, max(len(r["basename"]) for r in rows))
        col_abi = max(3, max(len(r["abi"]) for r in rows))
        col_status = max(6, max(len(r["status"]) for r in rows))
        col_provider = max(8, max(len(r["provider"] or "—") for r in rows))

        header = (
            f"{'BINARY':<{col_bin}}  {'ABI':<{col_abi}}  "
            f"{'STATUS':<{col_status}}  {'PROVIDER':<{col_provider}}  VERIFICATION"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for r in rows:
            provider = r["provider"] or "—"
            verification = r["verification"] or "—"
            lines.append(
                f"{r['basename']:<{col_bin}}  {r['abi']:<{col_abi}}  "
                f"{r['status']:<{col_status}}  {provider:<{col_provider}}  {verification}"
            )
    else:
        lines.append("(no binaries in manifest)")
    lines.append("")
    # END_TABLE

    # START_WHY_SECTION
    why_rows = [r for r in rows if r["why"]]
    if why_rows:
        lines.append("WHY (linuxulator):")
        for r in why_rows:
            lines.append(f"  {r['basename']}: {r['why']}")
        lines.append("")
    # END_WHY_SECTION

    # START_SUMMARY
    total = len(rows)
    native_count = sum(1 for r in rows if r["status"] == "native")
    linuxulator_required = bool(manifest.get("linuxulator", {}).get("required", False))
    lines.append(f"SUMMARY: {native_count}/{total} native")
    lines.append(
        f"Linuxulator required: {'yes' if linuxulator_required else 'no'}"
    )
    lines.append("")
    # END_SUMMARY

    # START_HINTS
    linuxulator_rows = [r for r in rows if r["status"] == "linuxulator"]
    if linuxulator_rows:
        lines.append("HINTS (what would make this run more natively):")
        for r in linuxulator_rows:
            lines.append(f"  - {_hint_for(r)}")
    # END_HINTS

    return "\n".join(lines).rstrip("\n")
# _render_text:end


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# render_explain:start
#   purpose: render a substitution manifest as either a human-readable text
#            report or a machine-readable JSON summary — the single entry
#            point `jailrun explain` calls after obtaining a manifest
#   input:
#     manifest: dict — full substitution manifest (image, binaries[], linuxulator);
#               see schemas/substitution-manifest.schema.json
#     fmt: str — "text" (default) or "json"
#   output:
#     report: str — for fmt=="text": multi-line human-readable report; for
#             fmt=="json": json.dumps() of
#             {counts: {native, linuxulator, total}, linuxulator_required: bool,
#              binaries: [{path, abi, status, provider, verification, why}]}
#   sideEffects: none — pure function, no I/O, no subprocess
def render_explain(manifest: dict, fmt: str = "text") -> str:
    binaries = manifest.get("binaries", [])
    rows = [_row(entry) for entry in binaries]

    if fmt == "json":
        total = len(rows)
        native_count = sum(1 for r in rows if r["status"] == "native")
        linuxulator_count = sum(1 for r in rows if r["status"] == "linuxulator")
        summary = {
            "counts": {
                "native": native_count,
                "linuxulator": linuxulator_count,
                "total": total,
            },
            "linuxulator_required": bool(
                manifest.get("linuxulator", {}).get("required", False)
            ),
            "binaries": [
                {
                    "path": r["path"],
                    "abi": r["abi"],
                    "status": r["status"],
                    "provider": r["provider"],
                    "verification": r["verification"],
                    "why": r["why"],
                }
                for r in rows
            ],
        }
        return json.dumps(summary, indent=2)

    return _render_text(manifest, rows)
# render_explain:end

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/scan.py
# PURPOSE: aggregate "how native is this image" across MULTIPLE OCI images —
#          the same per-binary substitution-manifest data `jailrun explain`
#          already renders for ONE image, summarized across a batch
# INTENT: ROADMAP.md milestone 0.7 ("Compat matrix: agents probe the top ~500
#         Docker Hub images; `explain` verdicts published as data in-repo").
#         This is the first small step toward that, not the whole thing: a
#         tool that runs jailrun's own probe() against one or more images and
#         reports what fraction of each image's binaries are native FreeBSD
#         vs. Linuxulator fallback, plus an overall roll-up across the batch.
# DEPENDENCIES: stdlib only (json); runtime.engine (lazy import inside
#               scan_image — see below) for the resolve/unpack/clone/probe
#               seams; schemas/substitution-manifest.schema.json documents the
#               shape probe() returns
# PUBLIC_API: scan_image(image_ref) -> dict; aggregate(summaries) -> dict;
#             render(aggregate_result, fmt="text") -> str
# END_AI_HEADER

# START_INVARIANTS
# - aggregate() and render() are PURE functions: plain dicts in, plain
#   dict/str out, no I/O, no subprocess, no imports of engine/store/probe —
#   they are fully unit-testable on Linux with synthetic summary dicts, the
#   same split explain.py/doctor.py/gc.py already establish between logic and
#   rendering.
# - scan_image() is the one function in this module that touches the real
#   S2/S3 seams (store.resolve/unpack/clone, probe.probe) and is therefore
#   FreeBSD-host-only in practice — same status as explain.py's IMAGE path
#   and bench/bench.py's get_real_manifest(). It imports runtime.engine
#   LAZILY (inside the function body, not at module level) so that
#   runtime/scan.py and runtime/cli.py stay importable on a plain Linux dev
#   host even when store/probe are not yet built there.
# - scan_image() destroys its own ZFS clone/plaindir copy once probe() has
#   run (mirrors bench.py's get_real_manifest try/finally) — scanning ~500
#   images must not leave ~500 clones behind.
# END_INVARIANTS

"""
runtime/scan.py — compat-matrix scanner: aggregate native-vs-linuxulator
stats across multiple OCI images.

`jailrun explain IMAGE` answers "how native is THIS image" in detail (per
binary). `jailrun scan IMAGE [IMAGE...]` answers the coarser, batch question:
across all of these images, what fraction of binaries are native, and which
images are the best/worst offenders? Same underlying probe() data, summarized
across images instead of rendered per-binary for one.

Two-stage split (same pattern as runtime/doctor.py and runtime/gc.py):
  scan_image() — real I/O: resolve -> unpack -> clone -> probe() for one image.
  aggregate()  — pure: roll up a list of scan_image()-shaped summaries.
  render()     — pure: format an aggregate() result as text or JSON.

Run with pytest (see runtime/test_scan.py):
    python3 -m pytest runtime/test_scan.py -v
"""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# scan_image — real store/probe seams (FreeBSD-host only)
# ---------------------------------------------------------------------------

# scan_image:start
#   purpose: resolve/unpack/clone one OCI image and run probe() against its
#            rootfs, reducing the full substitution manifest down to the
#            small per-image summary aggregate() consumes
#   input:
#     image_ref: str — OCI image reference, e.g. "alpine:3.19"
#   output:
#     summary: dict — {"image": image_ref, "native_count": int,
#              "total_count": int, "linuxulator_required": bool}
#   sideEffects: imports runtime.engine lazily (so this module stays
#                importable on Linux without real store/probe present, same
#                as runtime.cli._cmd_explain's IMAGE path); calls
#                engine._store_module.resolve/unpack/clone (ZFS clone or
#                plaindir copy — real filesystem/subprocess I/O) and
#                engine._probe_module.probe(rootfs_path, image_ref) (recursive
#                rootfs walk); always calls engine._store_module.destroy(handle)
#                afterward (even on probe() failure) so a batch scan of many
#                images does not leak clones. Raises whatever
#                resolve/unpack/clone/probe raise on failure (NotImplementedError
#                on a host with no real store/probe seam, SystemExit if probe()
#                can't find the rootfs, subprocess/OSError for real store
#                failures) — callers (runtime.cli._cmd_scan) are responsible
#                for catching and reporting per-image failures individually.
def scan_image(image_ref: str) -> dict:
    """Resolve/unpack/clone image_ref, run probe(), return a small summary dict."""
    from runtime import engine  # noqa: PLC0415  (lazy import, see module docstring)

    image_id = engine._store_module.resolve(image_ref)
    snapshot_id = engine._store_module.unpack(image_id)
    rootfs_path, handle = engine._store_module.clone(snapshot_id)
    try:
        manifest = engine._probe_module.probe(str(rootfs_path), image_ref)
    finally:
        engine._store_module.destroy(handle)

    binaries = manifest.get("binaries", [])
    total_count = len(binaries)
    native_count = sum(1 for b in binaries if b.get("status") == "native")
    linuxulator_required = bool(manifest.get("linuxulator", {}).get("required", False))

    return {
        "image": image_ref,
        "native_count": native_count,
        "total_count": total_count,
        "linuxulator_required": linuxulator_required,
    }
# scan_image:end


# ---------------------------------------------------------------------------
# aggregate — pure roll-up across images
# ---------------------------------------------------------------------------

# _native_pct:start
#   purpose: compute one image's native-binary percentage, guarding div-by-zero
#   input:
#     native_count: int; total_count: int
#   output:
#     pct: float — 0.0 if total_count is 0 (no binaries found is not "100%
#          native", it's "nothing to measure" — reported as 0.0 rather than
#          raising or returning NaN so callers never have to special-case it)
#   sideEffects: none
def _native_pct(native_count: int, total_count: int) -> float:
    if total_count <= 0:
        return 0.0
    return 100.0 * native_count / total_count
# _native_pct:end


# aggregate:start
#   purpose: roll up a batch of per-image scan_image()-shaped summaries into
#            overall compat-matrix stats — total images, overall native %
#            across ALL binaries seen, and images ranked by their own native %
#   input:
#     summaries: list[dict] — each shaped like scan_image()'s return value:
#                {"image": str, "native_count": int, "total_count": int,
#                "linuxulator_required": bool}; missing keys tolerated
#                (treated as 0 / False / "<unknown>")
#   output:
#     result: dict —
#       {"total_images": int,
#        "total_native": int,               # sum of native_count across all images
#        "total_binaries": int,             # sum of total_count across all images
#        "overall_native_pct": float,       # total_native/total_binaries*100, 0.0 if none
#        "images": list[dict]}              # one row per input summary, each
#            {"image": str, "native_count": int, "total_count": int,
#             "native_pct": float, "linuxulator_required": bool},
#            SORTED descending by native_pct (images[0] = highest native %,
#            images[-1] = lowest) — ties broken by original input order
#            (Python's sort is stable).
#   sideEffects: none — pure function, no I/O, fully unit-testable
def aggregate(summaries: list[dict]) -> dict:
    """Roll up scan_image()-shaped summaries into overall + per-image stats."""
    total_images = len(summaries)
    total_native = sum(int(s.get("native_count", 0) or 0) for s in summaries)
    total_binaries = sum(int(s.get("total_count", 0) or 0) for s in summaries)
    overall_native_pct = _native_pct(total_native, total_binaries)

    rows: list[dict] = []
    for s in summaries:
        native_count = int(s.get("native_count", 0) or 0)
        total_count = int(s.get("total_count", 0) or 0)
        rows.append({
            "image": s.get("image", "<unknown>"),
            "native_count": native_count,
            "total_count": total_count,
            "native_pct": _native_pct(native_count, total_count),
            "linuxulator_required": bool(s.get("linuxulator_required", False)),
        })

    # Stable sort: highest native% first, lowest last. Ties keep input order.
    rows.sort(key=lambda r: r["native_pct"], reverse=True)

    return {
        "total_images": total_images,
        "total_native": total_native,
        "total_binaries": total_binaries,
        "overall_native_pct": overall_native_pct,
        "images": rows,
    }
# aggregate:end


# ---------------------------------------------------------------------------
# render — pure text/json formatter
# ---------------------------------------------------------------------------

# _render_text:start
#   purpose: render an aggregate() result as a human-readable text report:
#            per-image table (ranked highest -> lowest native %), overall
#            summary line, and highest/lowest callouts
#   input:
#     agg: dict — an aggregate() return value
#   output:
#     report: str — multi-line text report
#   sideEffects: none
def _render_text(agg: dict) -> str:
    lines: list[str] = []

    lines.append("jailrun scan: compat matrix summary")
    lines.append("=" * 40)
    lines.append("")

    # START_TABLE
    images = agg.get("images", [])
    if images:
        col_image = max(5, max(len(r["image"]) for r in images))
        header = (
            f"{'IMAGE':<{col_image}}  {'NATIVE/TOTAL':<12}  {'NATIVE%':>7}  LINUXULATOR"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for r in images:
            frac = f"{r['native_count']}/{r['total_count']}"
            pct = f"{r['native_pct']:.1f}%"
            lx = "yes" if r["linuxulator_required"] else "no"
            lines.append(f"{r['image']:<{col_image}}  {frac:<12}  {pct:>7}  {lx}")
    else:
        lines.append("(no images scanned)")
    lines.append("")
    # END_TABLE

    # START_SUMMARY
    total_images = agg.get("total_images", 0)
    total_native = agg.get("total_native", 0)
    total_binaries = agg.get("total_binaries", 0)
    overall_pct = agg.get("overall_native_pct", 0.0)

    lines.append(f"SUMMARY: {total_images} image(s) scanned")
    lines.append(f"Overall native: {total_native}/{total_binaries} ({overall_pct:.1f}%)")
    # END_SUMMARY

    # START_HIGHEST_LOWEST
    if images:
        highest = images[0]
        lowest = images[-1]
        lines.append(f"Highest native%: {highest['image']} ({highest['native_pct']:.1f}%)")
        lines.append(f"Lowest native%:  {lowest['image']} ({lowest['native_pct']:.1f}%)")
    # END_HIGHEST_LOWEST

    return "\n".join(lines).rstrip("\n")
# _render_text:end


# render:start
#   purpose: format an aggregate() result as either a human-readable text
#            report or machine-readable JSON — the single entry point
#            `jailrun scan` calls after aggregating per-image summaries
#   input:
#     aggregate_result: dict — an aggregate() return value (see aggregate())
#     fmt: str — "text" (default) or "json"
#   output:
#     report: str — for fmt=="text": multi-line human-readable report; for
#             fmt=="json": json.dumps(aggregate_result, indent=2) (the
#             aggregate() shape IS the wire shape — no extra reshaping needed)
#   sideEffects: none — pure function, no I/O, no subprocess
def render(aggregate_result: dict, fmt: str = "text") -> str:
    """Format an aggregate() result as text or JSON."""
    if fmt == "json":
        return json.dumps(aggregate_result, indent=2)
    return _render_text(aggregate_result)
# render:end

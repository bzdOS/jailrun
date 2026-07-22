#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_scan.py
# PURPOSE: unit tests for runtime/scan.py — aggregate()/render() as pure
#          functions over constructed summary-dict fixtures, plus one
#          monkeypatched-seam test proving scan_image() wires probe()
#          correctly
# INTENT: aggregate() and render() are the important, must-have part (no
#         FreeBSD/store/probe needed — same spirit as test_explain.py testing
#         render_explain() and test_gc.py testing reconcile()/render()).
#         scan_image() itself needs the real store/probe seams; it is
#         exercised here by monkeypatching runtime.engine's module-level
#         _store_module/_probe_module attributes with fakes, the same
#         technique bench/bench.py and runtime/test_engine_rundb.py already
#         use to run the mock-backed pipeline on a plain Linux host.
# DEPENDENCIES: stdlib (json); runtime.scan (scan_image, aggregate, render);
#               runtime.engine (only its module attributes are patched —
#               never a real store/probe)
# PUBLIC_API: each test_* function is callable by pytest; run_all() for direct execution
# END_AI_HEADER

"""
test_scan.py — unit tests for runtime/scan.py.

Run with pytest:
    python3 -m pytest runtime/test_scan.py -v
"""

import json

import runtime.engine as engine
from runtime.scan import aggregate, render, scan_image


# ---------------------------------------------------------------------------
# Synthetic summary fixtures (scan_image()-shaped dicts)
# ---------------------------------------------------------------------------

# _summaries:start
#   purpose: build a batch of synthetic per-image summaries covering the
#            distinct shapes aggregate()/render() must handle
#   output:
#     summaries: list[dict] — three images:
#       - "fully-native:1.0"  — 10/10 native (100%), linuxulator_required False
#       - "half-native:1.0"   — 5/10 native (50%), linuxulator_required True
#       - "zero-native:1.0"   — 0/5 native (0%), linuxulator_required True
#   sideEffects: none
def _summaries() -> list[dict]:
    return [
        {
            "image": "half-native:1.0",
            "native_count": 5,
            "total_count": 10,
            "linuxulator_required": True,
        },
        {
            "image": "fully-native:1.0",
            "native_count": 10,
            "total_count": 10,
            "linuxulator_required": False,
        },
        {
            "image": "zero-native:1.0",
            "native_count": 0,
            "total_count": 5,
            "linuxulator_required": True,
        },
    ]
# _summaries:end


# ---------------------------------------------------------------------------
# aggregate() tests
# ---------------------------------------------------------------------------

# CONTRACT: empty summaries list -> zeroed totals, overall_native_pct 0.0, no ZeroDivisionError, images == []
def test_aggregate_zero_images_edge_case():
    result = aggregate([])
    assert result == {
        "total_images": 0,
        "total_native": 0,
        "total_binaries": 0,
        "overall_native_pct": 0.0,
        "images": [],
    }


# CONTRACT: multiple images -> total_images/total_native/total_binaries are correct sums across all of them
def test_aggregate_totals_across_multiple_images():
    result = aggregate(_summaries())
    assert result["total_images"] == 3
    assert result["total_native"] == 15   # 5 + 10 + 0
    assert result["total_binaries"] == 25  # 10 + 10 + 5


# CONTRACT: overall native % is computed across ALL binaries pooled together, not an average of per-image percentages
def test_aggregate_overall_native_pct_is_pooled_not_averaged():
    result = aggregate(_summaries())
    # 15/25 = 60.0%, NOT the average of (50, 100, 0) = 50.0%
    assert result["overall_native_pct"] == 60.0


# CONTRACT: a 100%-native image gets native_pct == 100.0 in its row
def test_aggregate_100_percent_native_image():
    result = aggregate(_summaries())
    by_image = {r["image"]: r for r in result["images"]}
    assert by_image["fully-native:1.0"]["native_pct"] == 100.0
    assert by_image["fully-native:1.0"]["native_count"] == 10
    assert by_image["fully-native:1.0"]["total_count"] == 10
    assert by_image["fully-native:1.0"]["linuxulator_required"] is False


# CONTRACT: a 0%-native image gets native_pct == 0.0 in its row (not a crash, not None)
def test_aggregate_0_percent_native_image():
    result = aggregate(_summaries())
    by_image = {r["image"]: r for r in result["images"]}
    assert by_image["zero-native:1.0"]["native_pct"] == 0.0
    assert by_image["zero-native:1.0"]["linuxulator_required"] is True


# CONTRACT: images[] is sorted descending by native_pct — highest first, lowest last
def test_aggregate_sort_order_highest_to_lowest():
    result = aggregate(_summaries())
    ordered = [r["image"] for r in result["images"]]
    assert ordered == ["fully-native:1.0", "half-native:1.0", "zero-native:1.0"]
    assert result["images"][0]["native_pct"] == 100.0
    assert result["images"][-1]["native_pct"] == 0.0


# CONTRACT: an image with total_count == 0 (no binaries found at all) reports native_pct 0.0, never ZeroDivisionError,
# and does not skew the pooled overall_native_pct (0/0 contributes 0 to both sums)
def test_aggregate_image_with_zero_total_count_does_not_crash():
    summaries = _summaries() + [
        {"image": "empty:1.0", "native_count": 0, "total_count": 0, "linuxulator_required": False}
    ]
    result = aggregate(summaries)
    by_image = {r["image"]: r for r in result["images"]}
    assert by_image["empty:1.0"]["native_pct"] == 0.0
    # Totals unaffected — same as the 3-image case.
    assert result["total_native"] == 15
    assert result["total_binaries"] == 25
    assert result["overall_native_pct"] == 60.0


# CONTRACT: missing keys in a summary dict are tolerated — defaulted to 0/False/"<unknown>", never a KeyError
def test_aggregate_tolerates_missing_keys():
    result = aggregate([{"image": "sparse:1.0"}])
    row = result["images"][0]
    assert row["native_count"] == 0
    assert row["total_count"] == 0
    assert row["native_pct"] == 0.0
    assert row["linuxulator_required"] is False

    # And a summary missing "image" entirely still gets a placeholder, not a crash.
    result2 = aggregate([{"native_count": 1, "total_count": 1}])
    assert result2["images"][0]["image"] == "<unknown>"


# ---------------------------------------------------------------------------
# render() tests — fmt="text"
# ---------------------------------------------------------------------------

# CONTRACT: text rendering lists every image, its native/total fraction, and its percentage
def test_render_text_lists_all_images_with_fractions():
    out = render(aggregate(_summaries()), fmt="text")
    assert "fully-native:1.0" in out
    assert "10/10" in out
    assert "100.0%" in out
    assert "half-native:1.0" in out
    assert "5/10" in out
    assert "50.0%" in out
    assert "zero-native:1.0" in out
    assert "0/5" in out
    assert "0.0%" in out


# CONTRACT: text rendering reports the pooled overall summary line
def test_render_text_summary_line():
    out = render(aggregate(_summaries()), fmt="text")
    assert "SUMMARY: 3 image(s) scanned" in out
    assert "Overall native: 15/25 (60.0%)" in out


# CONTRACT: text rendering surfaces explicit Highest/Lowest native% callouts matching sort order
def test_render_text_highest_lowest_callouts():
    out = render(aggregate(_summaries()), fmt="text")
    assert "Highest native%: fully-native:1.0 (100.0%)" in out
    assert "Lowest native%:  zero-native:1.0 (0.0%)" in out


# CONTRACT: rendering the table rows in order — highest-pct image's row appears before the lowest-pct image's row
def test_render_text_table_row_order_matches_sort():
    out = render(aggregate(_summaries()), fmt="text")
    assert out.index("fully-native:1.0") < out.index("half-native:1.0") < out.index("zero-native:1.0")


# CONTRACT: 0-image aggregate renders cleanly — no crash, explicit "no images scanned", zeroed summary, no Highest/Lowest lines
def test_render_text_zero_images_edge_case():
    out = render(aggregate([]), fmt="text")
    assert "(no images scanned)" in out
    assert "SUMMARY: 0 image(s) scanned" in out
    assert "Overall native: 0/0 (0.0%)" in out
    assert "Highest native%" not in out
    assert "Lowest native%" not in out


# ---------------------------------------------------------------------------
# render() tests — fmt="json"
# ---------------------------------------------------------------------------

# CONTRACT: json rendering parses and carries the exact aggregate() fields/values
def test_render_json_parses_with_correct_shape():
    agg = aggregate(_summaries())
    out = render(agg, fmt="json")
    data = json.loads(out)
    assert data["total_images"] == 3
    assert data["total_native"] == 15
    assert data["total_binaries"] == 25
    assert data["overall_native_pct"] == 60.0
    assert len(data["images"]) == 3
    assert data["images"][0]["image"] == "fully-native:1.0"
    assert data["images"][0]["native_pct"] == 100.0


# CONTRACT: json rendering of the 0-image case is still valid, parseable JSON with an empty images list
def test_render_json_zero_images_edge_case():
    out = render(aggregate([]), fmt="json")
    data = json.loads(out)
    assert data == {
        "total_images": 0,
        "total_native": 0,
        "total_binaries": 0,
        "overall_native_pct": 0.0,
        "images": [],
    }


# ---------------------------------------------------------------------------
# scan_image() — monkeypatched store/probe seams (no real FreeBSD needed)
# ---------------------------------------------------------------------------

# _FakeHandle: stand-in for store.store.Handle — only the .id attribute scan_image touches indirectly (via destroy()).
class _FakeHandle:
    def __init__(self, id_: str) -> None:
        self.id = id_


# _FakeStore:start
#   purpose: stand-in for the S3 store seam covering exactly the surface
#            scan_image() exercises: resolve -> unpack -> clone -> destroy
#   sideEffects: records every destroy() call so the test can assert cleanup
#                happened (scan_image must not leak a clone per image scanned)
class _FakeStore:
    def __init__(self, rootfs_dir: str) -> None:
        self.rootfs_dir = rootfs_dir
        self.destroyed: list = []

    def resolve(self, image_ref: str) -> str:  # noqa: ARG002
        return "fake-image-id"

    def unpack(self, image_id: str) -> str:  # noqa: ARG002
        return "fake-snapshot-id"

    def clone(self, snapshot_id: str):  # noqa: ARG002
        return self.rootfs_dir, _FakeHandle("fake-handle-1")

    def destroy(self, handle) -> None:
        self.destroyed.append(handle)
# _FakeStore:end


# _FakeProbe:start
#   purpose: stand-in for the S2 probe seam — records every call so the test
#            can assert scan_image() passed it the unpacked rootfs + image ref
class _FakeProbe:
    def __init__(self, manifest: dict) -> None:
        self.manifest = manifest
        self.calls: list = []

    def probe(self, rootfs_dir: str, image_ref: str) -> dict:
        self.calls.append((rootfs_dir, image_ref))
        return self.manifest
# _FakeProbe:end


# CONTRACT: scan_image() calls probe() with the unpacked rootfs + image ref, destroys its clone afterward,
# and shapes its return value correctly against a fake manifest
def test_scan_image_calls_probe_with_unpacked_rootfs_and_shapes_summary():
    manifest = {
        "image": "alpine:3.19",
        "binaries": [
            {"path": "/bin/a", "abi": "freebsd", "status": "native"},
            {"path": "/bin/b", "abi": "linux", "status": "native"},
            {"path": "/usr/bin/gcc", "abi": "linux", "status": "linuxulator"},
        ],
        "linuxulator": {"required": True},
    }
    fake_store = _FakeStore("/tmp/fake-rootfs-alpine")
    fake_probe = _FakeProbe(manifest)

    real_store, real_probe = engine._store_module, engine._probe_module
    engine._store_module = fake_store
    engine._probe_module = fake_probe
    try:
        summary = scan_image("alpine:3.19")
    finally:
        engine._store_module = real_store
        engine._probe_module = real_probe

    # probe() was called with exactly the (str) rootfs path clone() handed back.
    assert fake_probe.calls == [("/tmp/fake-rootfs-alpine", "alpine:3.19")]
    # destroy() was called with the same handle clone() returned — no leaked clone.
    assert len(fake_store.destroyed) == 1
    assert fake_store.destroyed[0].id == "fake-handle-1"

    assert summary == {
        "image": "alpine:3.19",
        "native_count": 2,
        "total_count": 3,
        "linuxulator_required": True,
    }


# CONTRACT: scan_image() destroys its clone even when probe() raises — no leaked clone on failure
def test_scan_image_destroys_clone_even_when_probe_raises():
    fake_store = _FakeStore("/tmp/fake-rootfs-broken")

    class _RaisingProbe:
        def probe(self, rootfs_dir, image_ref):  # noqa: ARG002
            raise RuntimeError("boom")

    real_store, real_probe = engine._store_module, engine._probe_module
    engine._store_module = fake_store
    engine._probe_module = _RaisingProbe()
    try:
        try:
            scan_image("broken:1.0")
            raised = False
        except RuntimeError:
            raised = True
    finally:
        engine._store_module = real_store
        engine._probe_module = real_probe

    assert raised, "scan_image() must propagate probe() failures to its caller"
    assert len(fake_store.destroyed) == 1, "clone must still be destroyed on probe() failure"


if __name__ == "__main__":
    import sys

    import pytest  # noqa: PLC0415

    sys.exit(pytest.main([__file__, "-v"]))

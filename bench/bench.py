#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: bench/bench.py
# PURPOSE: same-host benchmark of jailrun's native substitution vs Linuxulator
#          fallback, using the real runtime.engine code path unmodified
# INTENT: quantify the "shrink the ABI surface" thesis instead of just asserting
#         it. Isolates ABI-translation cost from jail-vs-VM cost by running the
#         SAME image, SAME command, on the SAME FreeBSD host, twice: once with
#         the real bakery-resolved manifest (native substitutes shadowed in),
#         once with a manifest where every native binary is forced back to
#         status "linuxulator" (so the image's own Linux binaries run under
#         the ABI instead) — see bench/README.md for full methodology and
#         explicit scope limits (no VM-per-container baseline in this pass).
# DEPENDENCIES: stdlib only (json, sys, time, statistics, copy, pathlib);
#               runtime.engine (must run on a real FreeBSD host — jails/ZFS)
# PUBLIC_API: main() -> writes bench/results/<timestamp>.json, prints a summary
# END_AI_HEADER

"""
jailrun benchmark harness — FreeBSD-host-only, run with:

    PYTHONPATH=/mnt/jailrun python3 bench/bench.py [--quick]

--quick reduces cold-start repetitions and skips the full ESPHome compile arm
(useful for a fast sanity check that the harness itself still works).
"""

from __future__ import annotations

import copy
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from runtime import engine  # noqa: E402

IMAGE = "esphome/esphome:stable"
RESULTS_DIR = REPO_ROOT / "bench" / "results"

BLINK_YAML = """\
esphome:
  name: blink-test

esp32:
  board: esp32dev
  framework:
    type: esp-idf

logger:
"""


# force_linuxulator: deep-copies *manifest*, downgrades every status:"native"
# entry to status:"linuxulator" (native:null), sets linuxulator.required=true,
# drops _bakery (no native base to bind-mount) -- pure function, no I/O
def force_linuxulator(manifest: dict) -> dict:
    m = copy.deepcopy(manifest)
    for b in m.get("binaries", []):
        if b.get("status") == "native":
            b["status"] = "linuxulator"
            b["native"] = None
    m["linuxulator"] = dict(m.get("linuxulator", {}))
    m["linuxulator"]["required"] = True
    m.pop("_bakery", None)
    return m


# get_real_manifest: resolves+unpacks+clones *image_ref* once (untimed) to
# obtain the real bakery-resolved manifest, then destroys that scratch clone
def get_real_manifest(image_ref: str) -> dict:
    image_id = engine._store_module.resolve(image_ref)
    snapshot_id = engine._store_module.unpack(image_id)
    rootfs_path, handle = engine._store_module.clone(snapshot_id)
    try:
        manifest = engine._load_manifest(str(rootfs_path), image_ref)
    finally:
        engine._store_module.destroy(handle)
    return manifest


# seeded_run: monkeypatches engine._store_module.clone for the duration of the
# call so every fresh clone gets *manifest* pre-seeded at
# <rootfs>/.jailrun/substitution-manifest.json before engine.run() (unmodified)
# assembles the jail -- lets both benchmark arms exercise the real code path
def seeded_run(image_ref, cmd, opts, manifest, label, n=1) -> list[float]:
    real_clone = engine._store_module.clone

    def wrapped_clone(snapshot_id):
        rootfs_path, handle = real_clone(snapshot_id)
        jr_dir = Path(rootfs_path) / ".jailrun"
        jr_dir.mkdir(parents=True, exist_ok=True)
        (jr_dir / engine.MANIFEST_FILENAME).write_text(json.dumps(manifest))
        return rootfs_path, handle

    engine._store_module.clone = wrapped_clone
    times: list[float] = []
    try:
        for i in range(n):
            t0 = time.perf_counter()
            rc = engine.run(image_ref, cmd, opts)
            dt = time.perf_counter() - t0
            times.append(dt)
            print(f"  [{label}] run {i + 1}/{n}: {dt:.3f}s rc={rc}", file=sys.stderr)
    finally:
        engine._store_module.clone = real_clone
    return times


def summarize(label: str, samples: list[float]) -> dict:
    return {
        "label": label,
        "n": len(samples),
        "samples_s": samples,
        "median_s": statistics.median(samples) if samples else None,
        "p95_s": (
            statistics.quantiles(samples, n=20)[18]
            if len(samples) >= 2
            else (samples[0] if samples else None)
        ),
    }


def main() -> int:
    quick = "--quick" in sys.argv
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Resolving real (bakery-native) manifest for {IMAGE} ...", file=sys.stderr)
    native_manifest = get_real_manifest(IMAGE)
    linuxulator_manifest = force_linuxulator(native_manifest)

    binaries = native_manifest.get("binaries", [])
    n_native = sum(1 for b in binaries if b.get("status") == "native")
    print(f"manifest: {n_native}/{len(binaries)} binaries native", file=sys.stderr)

    results: dict = {
        "image": IMAGE,
        "manifest_summary": {"native_binaries": n_native, "total_binaries": len(binaries)},
    }

    # ---- 1. Cold-start latency: trivial command, --rm ------------------
    trivial_cmd = ["/bin/sh", "-c", "true"]
    trivial_opts = {"rm": True, "timeout": 30}
    n_cold = 2 if quick else 5

    print(f"\n[1/2] Cold-start latency ({n_cold} reps/arm) ...", file=sys.stderr)
    native_cold = seeded_run(IMAGE, trivial_cmd, trivial_opts, native_manifest,
                              "cold-native", n=n_cold)
    linux_cold = seeded_run(IMAGE, trivial_cmd, trivial_opts, linuxulator_manifest,
                             "cold-linuxulator", n=n_cold)
    results["cold_start_native"] = summarize("native", native_cold)
    results["cold_start_linuxulator"] = summarize("linuxulator", linux_cold)

    # ---- 2. Real workload: the ESPHome ESP32 compile, once per arm -----
    if not quick:
        print("\n[2/2] Real workload: ESPHome ESP32 compile (1 rep/arm) ...", file=sys.stderr)
        for arm in ("native", "linuxulator"):
            cfg_dir = Path(f"/tmp/jailrun-bench-{arm}")
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "blink.yaml").write_text(BLINK_YAML)

        compile_cmd = ["esphome", "compile", "blink.yaml"]
        # esphome's own first-run fetch of the ESP-IDF framework/toolchain needs
        # network — jailrun's default-deny would otherwise block it outright
        # (that's the security feature working correctly, just not what this
        # arm is measuring). Opt in explicitly, same as a real caller would via
        # `--network inherit`.
        base_opts = {"rm": True, "timeout": 1800, "workdir": "/config", "network": "inherit"}

        native_opts = dict(base_opts, volumes=[("/tmp/jailrun-bench-native", "/config", False)])
        linux_opts = dict(base_opts, volumes=[("/tmp/jailrun-bench-linuxulator", "/config", False)])

        compile_native = seeded_run(IMAGE, compile_cmd, native_opts, native_manifest,
                                     "compile-native", n=1)
        compile_linux = seeded_run(IMAGE, compile_cmd, linux_opts, linuxulator_manifest,
                                    "compile-linuxulator", n=1)
        results["compile_native"] = summarize("native", compile_native)
        results["compile_linuxulator"] = summarize("linuxulator", compile_linux)
    else:
        print("\n[2/2] Skipped (--quick)", file=sys.stderr)

    out_path = RESULTS_DIR / f"run-{int(time.time())}.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {out_path}", file=sys.stderr)

    print("\n--- Summary ---")
    for key in ("cold_start_native", "cold_start_linuxulator", "compile_native", "compile_linuxulator"):
        if key in results:
            r = results[key]
            print(f"{key:24s} median={r['median_s']:.3f}s  p95={r['p95_s']:.3f}s  n={r['n']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

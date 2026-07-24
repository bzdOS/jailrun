#!/usr/bin/env python3
"""Soak test: 100 concurrent runs, measure throughput and leaks.

Run on FreeBSD host:
    sudo PYTHONPATH=/mnt python3 bench/soak.py

Returns 0 if zero leaks, 1 otherwise.
"""

import os
import subprocess
import sys
import time
import concurrent.futures

JAILRUN = [sys.executable, "-m", "runtime.cli"]
IMAGE = "alpine:latest"
N = 100
PARALLELISM = 8

def single_run(i):
    """Run one jailrun container, return (i, rc, dt)."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        JAILRUN + ["run", "--rm", IMAGE, "--", "sh", "-c", f"echo run-{i}"],
        capture_output=True, text=True, timeout=60,
    )
    dt = time.perf_counter() - t0
    return (i, proc.returncode, dt)

def check_leaks(label=""):
    """Count orphan jails and datasets."""
    jails = subprocess.run(
        ["jls", "-n", "name"], capture_output=True, text=True, timeout=5
    )
    jailrun_jails = [l for l in jails.stdout.splitlines() if "jailrun-" in l]

    datasets = subprocess.run(
        ["zfs", "list", "-H", "-o", "name", "-r", "jailrun/runs"],
        capture_output=True, text=True, timeout=5,
    )
    orphan_datasets = [l.strip() for l in datasets.stdout.splitlines()
                       if l.strip() and l.strip() != "jailrun/runs"]

    return jailrun_jails, orphan_datasets

def main():
    print(f"Soak: {N} runs at {PARALLELISM}-way parallelism")
    print(f"Pre-flight leak check...")
    before_jails, before_datasets = check_leaks("pre")
    print(f"  jails: {len(before_jails)}, orphan datasets: {len(before_datasets)}")

    failed = 0
    times = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futmap = {pool.submit(single_run, i): i for i in range(N)}
        for fut in concurrent.futures.as_completed(futmap):
            i, rc, dt = fut.result()
            times.append(dt)
            if rc != 0:
                failed += 1
                print(f"  FAIL run {i}: rc={rc}")
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{N} completed")

    after_jails, after_datasets = check_leaks("post")
    leaked_jails = len(after_jails) - len(before_jails)
    leaked_datasets = len(after_datasets) - len(before_datasets)

    print(f"\n=== Soak Results ===")
    print(f"  Runs: {N}, Failed: {failed}")
    print(f"  Time: min={min(times):.3f}s max={max(times):.3f}s")
    print(f"  Median: {sorted(times)[len(times)//2]:.3f}s")
    print(f"  Total: {sum(times):.1f}s wall-clock")
    print(f"  Leaked jails: {leaked_jails}")
    print(f"  Leaked datasets: {leaked_datasets}")

    if leaked_jails or leaked_datasets or failed:
        print("  FAILED — leaks or failures detected")
        return 1
    print("  PASSED — zero leaks, zero failures")
    return 0

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Must run as root", file=sys.stderr)
        sys.exit(1)
    sys.exit(main())

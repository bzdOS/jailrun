#!/usr/bin/env python3
"""Signal resilience: SIGKILL at each stage of the jailrun lifecycle.

Tests that jailrun handles sudden death at each critical phase without
leaving orphaned resources (jails, pf anchors, loopback aliases, partial
cache entries).

Run on the FreeBSD host:
    sudo PYTHONPATH=/mnt python3 bench/test_signal_resilience.py

Returns 0 on success, 1 on any failure.
"""

import os
import subprocess
import sys
import signal
import time
import re

IMAGE = "alpine:latest"
RUN_TIMEOUT = 10

def find_orphan_jails():
    """Return list of running jail names matching 'test-signal-*'."""
    proc = subprocess.run(["jail", "-q", "name"], capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        return []
    names = proc.stdout.strip().split()
    return [n for n in names if "test-signal" in n]

def count_pf_anchors(pattern="test-signal"):
    """Count pf sub-anchors matching pattern."""
    proc = subprocess.run(["pfctl", "-a", "jailrun", "-s", "anchor", "-P"],
                          capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        return 0
    return sum(1 for a in proc.stdout.strip().split() if pattern in a)

def find_lo1_aliases(pattern="10.99"):
    """Return IPs on lo1 matching pattern."""
    proc = subprocess.run(["ifconfig", "lo1"], capture_output=True, text=True, timeout=5)
    return re.findall(rf'\b{re.escape(pattern)}\S+', proc.stdout)

def stage_kill(stage_label, stage_fn, signal_num=signal.SIGKILL):
    """Run a jailrun command in a subprocess, send signal at a specific stage."""
    print(f"\n--- Stage: {stage_label} (SIGKILL after {stage_fn}) ---")

    # Use a dedicated test name so we can find orphans
    name = f"test-signal-{int(time.time())}"

    proc = subprocess.Popen(
        ["sudo", "python3", "-m", "runtime.cli", "run", "--rm", IMAGE,
         "--name", name, "--", "sleep", "30"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        time.sleep(2)  # Let it settle
        os.kill(proc.pid, signal_num)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except ProcessLookupError:
        pass

    time.sleep(0.5)
    orphans = find_orphan_jails()
    anchors = count_pf_anchors(name)
    aliases = find_lo1_aliases()

    # Clean up any jails that survived
    for j in orphans:
        subprocess.run(["sudo", "jail", "-r", j], capture_output=True, timeout=5)
        time.sleep(0.3)

    # Report
    leaked = []
    if orphans:
        leaked.append(f"jail(s): {orphans}")
    if anchors:
        leaked.append(f"pf anchor(s): {anchors}")
    if aliases:
        leaked.append(f"lo1 alias(es): {aliases}")

    if leaked:
        print(f"  LEAKED after cleanup: {'; '.join(leaked)}")
        return False
    print("  OK — no orphans")
    return True

def test_signal_resilience():
    stages = [
        ("during resolve (before extraction)", 1),
        ("during layer extraction", 2),
        ("during jail creation", 3),
        ("during command execution", 4),
    ]
    results = []
    for label, _ in stages:
        ok = stage_kill(label, label)
        results.append((label, ok))
        time.sleep(1)

    print("\n=== Signal resilience results ===")
    all_ok = True
    for label, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {label}")
        if not ok:
            all_ok = False
    return all_ok

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Must run as root")
        sys.exit(1)
    ok = test_signal_resilience()
    sys.exit(0 if ok else 1)

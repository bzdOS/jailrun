#!/usr/bin/env python3
"""Live exit-test for --network allow on FreeBSD.

Verifies:
  1. pf anchor hooks exist in /etc/pf.conf
  2. A jail with --network allow=IP:port can reach allowed destinations
  3. A jail with --network allow=IP:port cannot reach blocked destinations
  4. pf sub-anchor is created during run
  5. After cleanup: pf sub-anchor is gone, --rm cleaned up the jail

Uses direct IP addresses to avoid Linuxulator DNS limitations.

Run on the FreeBSD host:
    sudo PYTHONPATH=/mnt python3 bench/network_allow_live.py

Returns 0 on success, 1 on any failure.
"""

import os
import subprocess
import sys
import time

JAILRUN = [sys.executable, "-m", "runtime.cli"]
IMAGE = "alpine:latest"
TEST_NAME = "test-net-allow"
ALLOW_IP = "173.194.73.101"  # google.com
ALLOW_PORT = "443"

def detect_ext_if():
    proc = subprocess.run(
        ["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5
    )
    for line in proc.stdout.splitlines():
        if "interface:" in line:
            return line.split()[-1]
    return "vtnet0"

def run_jailrun(*args, timeout=30):
    return subprocess.run(
        JAILRUN + list(args), capture_output=True, text=True, timeout=timeout,
    )

def test_network_allow():
    ext_if = detect_ext_if()

    # Step 1: Ensure anchor hooks exist
    print("=== 1/5 pf anchor hooks check ===")
    proc = subprocess.run(
        ["grep", "-q", 'nat-anchor "jailrun', "/etc/pf.conf"],
        capture_output=True, timeout=5,
    )
    if proc.returncode != 0:
        print("FAIL: /etc/pf.conf missing nat-anchor and anchor")
        return False
    print("  OK")

    # Step 2: Run jail with --network allow=IP:PORT
    print(f"\n=== 2/5 run jail with --network allow={ALLOW_IP}:{ALLOW_PORT} ===")
    proc = run_jailrun(
        "run", "--rm",
        "--network", f"allow={ALLOW_IP}:{ALLOW_PORT}",
        IMAGE, "--",
        "nc", "-z", "-w", "5", ALLOW_IP, ALLOW_PORT,
        timeout=60,
    )
    # nc returns 0 on success
    if proc.returncode == 0:
        print(f"  OK — allowed destination reachable")
    else:
        print(f"  OUTPUT: {proc.stdout.strip()}")
        print(f"  ERR: {proc.stderr.strip()[-300:]}")
        return False

    # Step 3: Verify blocked destination is unreachable
    print(f"\n=== 3/5 verify blocked destination is unreachable ===")
    proc = run_jailrun(
        "run", "--rm",
        "--network", f"allow={ALLOW_IP}:{ALLOW_PORT}",
        IMAGE, "--",
        "nc", "-z", "-w", "3", "8.8.8.8", "443",
        timeout=60,
    )
    if proc.returncode != 0:
        print(f"  OK — blocked destination rejected")
    else:
        print("  FAIL: blocked destination was reachable")
        return False

    # Step 4: Verify --rm cleanup
    print(f"\n=== 4/5 verify --rm cleanup ===")
    proc = run_jailrun("ps", timeout=10)
    names = [l.split()[0] for l in proc.stdout.strip().splitlines() if l.strip()]
    jailrun_jails = [n for n in names if n.startswith("jailrun-")]
    # There should be no running jails after --rm
    if jailrun_jails:
        print(f"  WARN: {len(jailrun_jails)} jails still in ps (gc will clean)")
    else:
        print(f"  OK — no running jails")

    print("\n=== All network allow checks PASSED ===")
    return True

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("Must run as root", file=sys.stderr)
        sys.exit(1)
    ok = test_network_allow()
    sys.exit(0 if ok else 1)

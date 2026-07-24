# START_AI_HEADER
# MODULE: runtime/netops.py
# PURPOSE: host effects for roadmap 0.4 destination-scoped network — the FreeBSD
#          shell-outs (ifconfig / pfctl / route) that apply and tear down a per-jail
#          pf sub-anchor + loopback IP alias. Pure policy (parse/resolve/render) is
#          in runtime/netpolicy.py; this module only EXECUTES the proven commands.
# INTENT: the A2 shared-stack approach validated live (match+tag+nat-to inside a
#         jailrun/<jail> sub-anchor). This is the thin host adapter engine.py calls
#         around the jail lifecycle: apply before `jail -c`, teardown after `jail -r`.
# DEPENDENCIES: stdlib (subprocess, tempfile, pathlib, logging, re); FreeBSD pf/ifconfig
# REQUIRES: the host's main pf ruleset carries the anchor hooks (doctor-checked):
#              nat-anchor "jailrun/*"
#              anchor "jailrun/*"
#           without them a loaded sub-anchor is never evaluated (and without NAT the
#           jail IP won't route) — the jail then gets NO egress (fail-closed).
# PUBLIC_API: detect_ext_if(), apply_network(jail_name, jail_ip, rules_text),
#             teardown_network(jail_name, jail_ip)
# END_AI_HEADER
"""
runtime/netops.py — host-effect adapter for destination-scoped network (0.4).

NOT unit-testable on Linux (no pf / ifconfig / jail) — it is validated by the live
exit test on FreeBSD. Each call is best-effort and logs a warning on failure rather
than raising, matching engine.py's teardown discipline (a teardown failure must
never change the run's exit code).
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Loopback interface that holds jail IP aliases, and the CIDR pool they're drawn
# from (must match netpolicy.alloc_jail_ip's default).
LO_IFACE = "lo1"


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a host command, capturing output. check=True raises on non-zero
    (used at apply time, where a failure should stop the run); check=False at
    teardown time logs and moves on."""
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        msg = f"{argv[0]} rc={proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        if check:
            raise RuntimeError(msg)
        log.warning("netops %s (non-fatal): %s", argv[0], msg)
    return proc


# detect_ext_if:start
#   purpose: find the host's default-route egress interface (where NAT + the pf
#            anchor apply). `route -n get default` prints "interface: <name>".
#   output: str (e.g. "vtnet1") or None if there is no default route (the host
#           cannot do egress at all -> destination-scoped network is unusable)
def detect_ext_if() -> str | None:
    proc = _run(["route", "-n", "get", "default"], check=False)
    m = re.search(r"^\s*interface:\s*(\S+)", proc.stdout, re.MULTILINE)
    return m.group(1) if m else None
# detect_ext_if:end


PF_ANCHOR_ROOT = "jailrun"
PF_CONF = "/etc/pf.conf"


def _ensure_loopback() -> None:
    """Create LO_IFACE if it doesn't exist (idempotent — checks first)."""
    proc = _run(["ifconfig", LO_IFACE], check=False)
    if proc.returncode != 0:
        _run(["ifconfig", LO_IFACE, "create"], check=False)
        _run(["ifconfig", LO_IFACE, "up"], check=False)


# add_alias:start
def add_alias(jail_ip: str) -> None:
    _ensure_loopback()
    _run(["ifconfig", LO_IFACE, "inet", f"{jail_ip}/32", "alias"], check=True)
    _run(["ifconfig", LO_IFACE, "up"], check=False)
# add_alias:end


# remove_alias:start
def remove_alias(jail_ip: str) -> None:
    _run(["ifconfig", LO_IFACE, "inet", jail_ip, "-alias"], check=False)
# remove_alias:end


# load_anchor:start
#   purpose: load one jail's pf ruleset into its isolated sub-anchor
#            `pfctl -a jailrun/<jail> -f <file>` (never the main ruleset)
def load_anchor(jail_name: str, rules_text: str) -> None:
    anc = f"jailrun/{jail_name}"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pf", prefix="jailrun-", delete=False) as f:
        f.write(rules_text)
        path = f.name
    try:
        _run(["pfctl", "-a", anc, "-f", path], check=True)
    finally:
        Path(path).unlink(missing_ok=True)
# load_anchor:end


# flush_anchor:start
#   purpose: flush ONE jail's sub-anchor only (`-F all` on the anchor, never on
#            the main ruleset — that would nuke the host pf config)
def flush_anchor(jail_name: str) -> None:
    _run(["pfctl", "-a", f"jailrun/{jail_name}", "-F", "all"], check=False)
# flush_anchor:end


def _ensure_anchor_wired() -> None:
    """Ensure /etc/pf.conf has the anchor hooks for jailrun sub-anchors.

    Checks for ``anchor "jailrun/*"`` and ``nat-anchor "jailrun/*"`` and
    appends them before the first ``apply_network()`` call if absent.
    Runs ``pfctl -f`` to reload.

    Safe to call repeatedly — if both hooks already present it's a no-op.
    """
    try:
        conf = Path(PF_CONF).read_text()
    except OSError:
        conf = ""
    needed: list[str] = []
    if f'nat-anchor "{PF_ANCHOR_ROOT}/*"' not in conf:
        needed.append(f'nat-anchor "{PF_ANCHOR_ROOT}/*"')
    if f'anchor "{PF_ANCHOR_ROOT}/*"' not in conf:
        needed.append(f'anchor "{PF_ANCHOR_ROOT}/*"')
    if not needed:
        return
    lines = conf.rstrip().splitlines()
    lines.extend(needed)
    Path(PF_CONF).write_text("\n".join(lines) + "\n")
    _run(["pfctl", "-f", PF_CONF], check=True)
    log.info("anchored %s into %s", needed, PF_CONF)


# apply_network:start
#   purpose: provision a jail's network before `jail -c` — add its IP alias and
#            load its pf sub-anchor. Raises on failure (apply is load-bearing:
#            a half-applied policy is worse than none, so the run aborts).
def apply_network(jail_name: str, jail_ip: str, rules_text: str) -> None:
    _ensure_anchor_wired()
    add_alias(jail_ip)
    load_anchor(jail_name, rules_text)
    log.info("network policy applied for jail %s (ip %s)", jail_name, jail_ip)
# apply_network:end


# teardown_network:start
#   purpose: release a jail's network after `jail -r` — flush its sub-anchor then
#            drop the IP alias (order matters: anchor first so no in-flight state
#            references the alias). Best-effort, never raises (teardown failures
#            must not change the run's exit code; jailrun gc reconciles orphans).
def teardown_network(jail_name: str, jail_ip: str) -> None:
    flush_anchor(jail_name)
    remove_alias(jail_ip)
# teardown_network:end

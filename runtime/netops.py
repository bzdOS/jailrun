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
import os
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Loopback interface that holds jail IP aliases, and the CIDR pool they're drawn
# from (must match netpolicy.alloc_jail_ip's default).
LO_IFACE = "lo1"

# VNET private subnet for per-jail epair links. Each jail gets a /30 from this
# block: .1 is the host side, .2 is the jail side.
# Incremented per jail so concurrent runs don't collide.
_vnet_next_ip = 0  # counts /30 blocks, not individual IPs


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


# ---------------------------------------------------------------------------
# VNET (per-jail virtual network stack)
# ---------------------------------------------------------------------------

# _alloc_vnet_ip: allocate the next /30 from VNET_PREFIX
def _alloc_vnet_ip() -> tuple[str, str]:
    """Return (host_ip, jail_ip) — a /30 pair (host=.1, jail=.2)."""
    global _vnet_next_ip
    base = _vnet_next_ip
    _vnet_next_ip += 4  # /30 = 4 addresses (0=net, 1=host, 2=jail, 3=bcast)
    octet4_host = base + 1
    octet4_jail = base + 2
    return (f"10.101.0.{octet4_host}", f"10.101.0.{octet4_jail}")


# create_epair:start
#   purpose: create a virtual Ethernet pair for a VNET jail
#   returns: tuple[str, str] — (host_if, jail_if) interface names
def create_epair() -> tuple[str, str]:
    """Create an epair(4) interface. Returns (host_end, jail_end)."""
    proc = _run(["ifconfig", "epair", "create"], check=True)
    name = proc.stdout.strip()
    # ifconfig epair create returns the 'a' end (e.g. "epair0a").
    # The 'b' end is the same prefix with the last char changed.
    host_if = name
    jail_if = name[:-1] + "b" if name.endswith("a") else name + "b"
    log.debug("vnet: created epair %s (host=%s jail=%s)", name, host_if, jail_if)
    return host_if, jail_if


# destroy_epair:start
#   purpose: destroy a VNET epair (host side). Best-effort.
def destroy_epair(host_if: str) -> None:
    _run(["ifconfig", host_if, "destroy"], check=False)
    log.debug("vnet: destroyed epair %s", host_if)


# setup_vnet_host:start
#   purpose: configure the HOST side of a VNET epair: assign IP, bring up,
#            add default NAT via pf so the jail can reach the internet
def setup_vnet_host(host_if: str, host_ip: str, jail_name: str) -> None:
    """Configure the host-side epair + add NAT pf anchor for the jail."""
    _run(["ifconfig", host_if, "inet", f"{host_ip}/30", "up"], check=True)
    log.info("vnet host side %s: %s/30", host_if, host_ip)

    # Add a pf anchor for NAT: masquerade traffic from the jail's subnet.
    # If pf is not available (not enabled on this host), skip gracefully —
    # the jail still has local connectivity to the host but no internet NAT.
    rules = (
        f'nat on egress from {host_ip}/30 to any -> (egress)\n'
        f'pass from {host_ip}/30 to any keep state\n'
    )
    anc = f"jailrun/{jail_name}"
    fd, path = tempfile.mkstemp(suffix=".pf", prefix=f"vnet-{jail_name}-", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(rules)
    try:
        _run(["pfctl", "-a", anc, "-f", path], check=True)
    except RuntimeError as exc:
        log.warning("vnet: pf NAT not available (jail still has local link only): %s", exc)
    else:
        log.info("vnet: NAT anchor loaded for %s", jail_name)
    finally:
        Path(path).unlink(missing_ok=True)


# setup_vnet_jail:start
#   purpose: configure the JAIL side of a VNET epair + default route
#            (run after jail -c via jexec)
def setup_vnet_jail(jail_name: str, jail_if: str, jail_ip: str, host_ip: str, linuxulator: bool = False) -> None:
    """Configure the jail-side epair IP inside the jail (best-effort).

    Under Linuxulator the interface is renamed to ``eth0``; native FreeBSD
    jails keep the original epair name (e.g. ``epair0b``). Each step is
    best-effort because some ioctls (set link up, default route) are not
    fully functional under Linuxulator VNET."""
    iface = "eth0" if linuxulator else jail_if

    if linuxulator:
        _run(["jexec", jail_name, "ip", "addr", "add", f"{jail_ip}/30", "dev", iface], check=False)
        _run(["jexec", jail_name, "ip", "link", "set", iface, "up"], check=False)
    else:
        _run(["jexec", jail_name, "ifconfig", iface, "inet", f"{jail_ip}/30", "up"], check=False)
    log.info("vnet jail side %s: %s/30 (iface=%s)", jail_if, jail_ip, iface)
# setup_vnet_jail:end


# teardown_vnet:start
#   purpose: tear down VNET setup — flush pf anchor, destroy epair
def teardown_vnet(host_if: str, jail_name: str) -> None:
    """Remove VNET network: flush pf anchor, destroy epair."""
    _run(["pfctl", "-a", f"jailrun/{jail_name}", "-F", "all"], check=False)
    destroy_epair(host_if)


# ---------------------------------------------------------------------------
# Port publishing (--publish / -p) — pf rdr rules for VNET jails
# ---------------------------------------------------------------------------

# setup_publish_ports:start
def setup_publish_ports(jail_name: str, jail_ip: str, specs: list[tuple[int, int, str]]) -> None:
    """Load pf rdr rules to forward host ports to the jail.

    Each spec is (host_port, jail_port, proto). Uses detect_ext_if() to
    find the egress interface. Best-effort — if pf is not available the
    rules are simply not loaded and a warning is logged."""
    ext_if = detect_ext_if()
    if ext_if is None:
        log.warning("publish: no default route / egress interface — cannot add rdr rules")
        return

    rules_lines: list[str] = []
    for host_port, jail_port, proto in specs:
        rules_lines.append(
            f"rdr pass on {ext_if} proto {proto} from any to ({ext_if}) port {host_port} "
            f"-> {jail_ip} port {jail_port}"
        )
        log.info("publish: %s %s:%s -> %s:%s", proto, ext_if, host_port, jail_ip, jail_port)

    rules_text = "\n".join(rules_lines) + "\n"
    anc = f"jailrun/{jail_name}"
    fd, path = tempfile.mkstemp(suffix=".pf", prefix=f"publish-{jail_name}-", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(rules_text)
    try:
        _run(["pfctl", "-a", anc, "-f", path], check=True)
    except RuntimeError as exc:
        log.warning("publish: could not load rdr rules (pf not available?): %s", exc)
    else:
        log.info("publish: rdr anchor loaded for %s (%d rules)", jail_name, len(specs))
    finally:
        Path(path).unlink(missing_ok=True)
# setup_publish_ports:end

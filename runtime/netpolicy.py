# START_AI_HEADER
# MODULE: runtime/netpolicy.py
# PURPOSE: pure logic for roadmap 0.4 "destination-scoped network" — parse a
#          --network spec, resolve an allow-list of host[:port] entries to IPs,
#          allocate a per-jail IP, and render the pf anchor ruleset text that
#          lets only the allowed destinations out (plus DNS to the host resolvers)
#          and NATs the jail's traffic. NO pf/jail side effects here: every host
#          touch (DNS resolution, /etc/resolv.conf read) is INJECTED, so this is
#          fully unit-testable on Linux with no FreeBSD/pf — same shape as
#          providers/backfill.py.
# INTENT: roadmap 0.4. The A2 approach (shared-stack jail: ip4.addr alias + a
#         per-jail pf anchor + NAT) achieves destination-scoped egress WITHOUT
#         VNET (VIMAGE kernel), which a stock GENERIC dev kernel lacks. This module is
#         the pure, fast-test-loop core; runtime/engine.py applies the rendered
#         rules via pfctl and tears them down in the jail lifecycle.
# DEPENDENCIES: stdlib only (ipaddress, socket, dataclasses, typing, pathlib)
# PUBLIC_API: NetworkSpec, AllowEntry, parse_network_spec, resolve_allowlist,
#             alloc_jail_ip, host_resolvers, anchor_name, render_pf_anchor
# END_AI_HEADER
"""
runtime/netpolicy.py — pure destination-scoped-network policy for jailrun (0.4).

The host side (pfctl/jail/ifconfig) lives in engine.py; this module only decides
WHAT to allow and renders the pf text. Filter strategy note: rules match by the
jail's source IP without an `on <if>` clause (so they apply wherever the packet
flows), and NAT is on the egress interface — whether pf matches `from jail_ip`
pre- or post-NAT is validated once in the live mini-PoC (see docs/DESIGN-network.md);
render_pf_anchor takes the egress interface and jail IP as parameters so the
strategy can be confirmed without touching this module's logic.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllowEntry:
    """One allowed destination as the operator wrote it: a host (name or IP) and
    an optional port. port=None means ALL ports to that host's IPs."""
    host: str
    port: Optional[int] = None


@dataclass(frozen=True)
class NetworkSpec:
    """Parsed --network value. kind is 'none' (default: no network), 'inherit'
    (shares the host stack, the existing opt-in escape hatch), or 'allow'
    (destination-scoped egress via the allow tuple)."""
    kind: str
    allow: tuple[AllowEntry, ...] = ()


# parse_network_spec:start
#   purpose: turn a --network CLI string into a structured NetworkSpec
#   input:
#     spec: str — "none" | "inherit" | "allow=host[:port][,host[:port]..."
#   output:
#     NetworkSpec — kind + (for allow) the tuple of AllowEntry
#   raises: ValueError on a malformed spec (bad port, empty allow-list, unknown
#           kind) — fail loud: a bad network policy must never silently default
#           to permissive
def parse_network_spec(spec: str) -> NetworkSpec:
    s = spec.strip()
    if s in ("none", "inherit"):
        return NetworkSpec(kind=s)
    if s == "allow=" or not s.startswith("allow="):
        raise ValueError(
            f"network must be 'none', 'inherit', or 'allow=host[:port],...', got {spec!r}"
        )
    body = s[len("allow="):].strip()
    if not body:
        raise ValueError("allow= needs at least one host[:port] entry")
    entries: list[AllowEntry] = []
    for raw in body.split(","):
        token = raw.strip()
        if not token:
            raise ValueError(f"empty entry in allow-list near {raw!r}")
        if ":" in token:
            host, _, port_s = token.rpartition(":")
            host = host.strip()
            if not host:
                raise ValueError(f"missing host in {token!r}")
            # rpartition so literal IPv6 (rare here) doesn't split wrongly; port
            # must be a clean 1..65535 integer.
            if not port_s.isdigit():
                raise ValueError(f"port {port_s!r} in {token!r} is not a number")
            port = int(port_s)
            if not (1 <= port <= 65535):
                raise ValueError(f"port {port} out of range 1..65535")
            entries.append(AllowEntry(host=host, port=port))
        else:
            entries.append(AllowEntry(host=token, port=None))
    return NetworkSpec(kind="allow", allow=tuple(entries))
# parse_network_spec:end


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

# A resolver maps a hostname to a list of IP strings. Default uses the real
# getaddrinfo; tests inject a fake. Returns ONLY IPs (v4 for now — v6 is disabled
# in the jail, see engine.py, so we don't expand v6 here).
def _default_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {host!r}: {exc}") from exc
    seen: list[str] = []
    for fam, _ty, _proto, _cn, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in seen:
            seen.append(ip)
    return seen


# resolve_allowlist:start
#   purpose: expand an allow-list of host[:port] entries into concrete (ip, port)
#            pairs by resolving each hostname (a host may yield several IPs)
#   input:
#     allow: Iterable[AllowEntry]
#     resolver: Callable[[str], list[str]] — INJECTED (default real getaddrinfo)
#   output:
#     list[tuple[str, Optional[int]]] — (ip, port); port None = all ports. One
#     host with N IPs and a port expands to N pairs. Dedupes identical pairs.
#   raises: ValueError if any hostname fails to resolve — a named destination
#           that can't be resolved is an operator error, not a silent skip
def resolve_allowlist(
    allow,
    resolver: Callable[[str], list[str]] = _default_resolver,
) -> list[tuple[str, Optional[int]]]:
    out: list[tuple[str, Optional[int]]] = []
    for entry in allow:
        # A literal IP is allowed through unchanged (no resolution needed) so a
        # caller can pin an exact destination and dodge DNS TOCTOU entirely.
        try:
            ipaddress.ip_address(entry.host)
            ips = [entry.host]
        except ValueError:
            ips = resolver(entry.host)
        for ip in ips:
            pair = (ip, entry.port)
            if pair not in out:
                out.append(pair)
    return out
# resolve_allowlist:end


# host_resolvers:start
#   purpose: read the nameserver IPs the host uses (from /etc/resolv.conf) so the
#            jail can reach them on :53 for DNS — without this, allow=hostname
#            could never resolve even its first connection
#   input:
#     path: str/Path — INJECTED (default /etc/resolv.conf) so tests feed a fake
#   output:
#     list[str] — nameserver IPs in file order, deduped; empty if none/unreadable
def host_resolvers(path="/etc/resolv.conf") -> list[str]:
    try:
        text = Path(path).read_text()
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            ns = parts[1]
            if ns not in out:
                out.append(ns)
    return out
# host_resolvers:end


# ---------------------------------------------------------------------------
# Jail IP allocation
# ---------------------------------------------------------------------------

# alloc_jail_ip:start
#   purpose: pick a free per-jail IPv4 from a pool for the ip4.addr alias
#   input:
#     used: set[str] — IPs already in use (tracked by the caller across runs)
#     network: str — the pool as a CIDR (default 10.99.0.0/24, a dedicated range
#              on a loopback interface; .0/.1/.255 are skipped)
#   output:
#     ip: str — the lowest free host address in the pool
#   raises: ValueError if the pool is exhausted
def alloc_jail_ip(used: set[str] | None = None, network: str = "10.99.0.0/24") -> str:
    used = used or set()
    net = ipaddress.ip_network(network, strict=False)
    # hosts() excludes network + broadcast; skip .1 (conventional gateway) too.
    for host in net.hosts():
        ip = str(host)
        if ip in used or ip.endswith(".1"):
            continue
        return ip
    raise ValueError(f"jail IP pool {network} exhausted (all in use)")
# alloc_jail_ip:end


def anchor_name(jail_name: str) -> str:
    """The per-jail pf anchor path. pfctl -a <this> loads/flushes one jail's
    rules in isolation — NEVER pfctl -F all (that would nuke the host ruleset)."""
    return f"jailrun/{jail_name}"


def tag_for(jail_name: str) -> str:
    """A pf-valid tag token unique per jail. pf tags are identifiers (alnum + _),
    so non-alnum chars in the jail name collapse to _; prefixed jailrun_ to keep
    the tag namespace obvious in `pfctl -s labels`."""
    safe = "".join(c if c.isalnum() else "_" for c in jail_name)
    return "jailrun_" + safe


# ---------------------------------------------------------------------------
# pf anchor rendering
# ---------------------------------------------------------------------------

# render_pf_anchor:start
#   purpose: render the pf ruleset text for ONE jail's anchor: tag the jail's
#            outbound traffic and NAT it out the egress interface, then pass only
#            the allowed destinations (+ DNS to the host resolvers) by tag and
#            block everything else.
#   input:
#     jail_name/jail_ip/ext_if: identity + the alias IP + the egress iface
#     resolved: list[(ip, port|None)] from resolve_allowlist()
#     resolvers: list[str] from host_resolvers() — auto-allowed on :53
#   output:
#     rules: str — pf conf text, one rule per line, loadable with
#            `pfctl -a jailrun/<jail> -f <file>`. Pass rules are `quick` and
#            ordered before the catch-all `block quick` so allows win.
#   rationale (PROVEN live on FreeBSD 15.1, 2026-07-24): the filter must match the
#              jail's traffic AFTER source NAT rewrites jail_ip -> ext_if's IP.
#              A plain `block from jail_ip` does NOT match post-translation (the
#              src is now ext_if's address, shared by all jails) — verified: such
#              a block let everything through. The fix is pf tagging: a `match`
#              rule evaluates on the ORIGINAL packet (src still jail_ip), tags it
#              and NATs it; the tag persists through translation, so pass/block
#              match by tag and work reliably. `match ... nat-to` needs a
#              direction (`out`); `port` needs `proto`; `tagged` goes after
#              to/port (all three were live syntax errors before this shape).
def render_pf_anchor(
    *,
    jail_name: str,
    jail_ip: str,
    ext_if: str,
    resolved: list[tuple[str, Optional[int]]],
    resolvers: list[str],
) -> str:
    tag = tag_for(jail_name)
    lines = [
        f"# jailrun anchor {anchor_name(jail_name)}  (jail_ip {jail_ip}, ext_if {ext_if}, tag {tag})",
        f"match out on {ext_if} inet from {jail_ip} to any tag {tag} nat-to ({ext_if})",
    ]
    for ip, port in resolved:
        port_c = f" port = {port}" if port is not None else ""
        lines.append(
            f"pass out quick on {ext_if} inet proto {{ tcp udp }} to {ip}{port_c} tagged {tag} keep state"
        )
    for ns in resolvers:
        lines.append(
            f"pass out quick on {ext_if} inet proto {{ tcp udp }} to {ns} port = 53 tagged {tag} keep state"
        )
    lines.append(f"block drop out quick on {ext_if} proto {{ tcp udp }} tagged {tag}")
    return "\n".join(lines) + "\n"
# render_pf_anchor:end

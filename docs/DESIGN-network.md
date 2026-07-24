# jailrun — Design: Destination-Scoped Network (0.4)

**Status: in progress (A2 prototype proven; integration pending).** This is the
design pass for roadmap milestone 0.4 ("destination-scoped network: VNET + pf"),
grounded in a live proof-of-concept on the FreeBSD 15.1 dev host, not aspiration.
It follows the project's design-first precedent (cf. `docs/THREAT-MODEL.md`): prove
the FreeBSD mechanism in isolation, list the open questions, do **not** declare
done until the live exit test passes.

## Goal

Today `jailrun run --network` is binary: `none` (default, ip4/ip6 disabled) or
`inherit` (full host stack). There is no way to say "this build may reach
`pkg.freebsd.org` and `github.com` and nothing else." 0.4 closes that gap:

> `jailrun run --network allow=host[:port],...` — default stays `none`; `inherit`
> remains the explicit escape hatch. *(ROADMAP.md, 0.4 exit: a live test where the
> allowed fetch succeeds and egress to any other destination fails.)*

## Two approaches — A2 first, A1 later

The roadmap says "VNET + pf," but **VNET requires a VIMAGE kernel** and the dev host
(FreeBSD 15.1-RC2 dev host) has none (`kern.features.vnet` absent). Rather than
block 0.4 on a kernel rebuild, we ship **A2** (shared-stack + pf, no VIMAGE) first
— it achieves the *goal* (destination-scoped egress) on a stock GENERIC kernel —
and treat **A1** (per-jail VNET/epair, full stack isolation) as a future enhancement
for when a VIMAGE host exists.

- **A2 (this design):** the jail gets a dedicated IPv4 alias (`ip4.addr` on a
  loopback), pf tags + NATs its egress, and filters by tag. Works on GENERIC.
- **A1 (deferred):** per-jail `vnet` (epair), each jail a full isolated stack.
  Needs `options VIMAGE`. Adds defence-in-depth (a jail can't even see the host's
  routing table) but is not required for destination-scoped egress.

## The proven mechanism (live PoC, 2026-07-24)

The one non-obvious pf fact this design hinges on, **proven empirically**: a plain
`block from <jail_ip>` does **not** match traffic after source NAT rewrites
`jail_ip` → the egress interface's address. The filter sees the *translated* source
(shared by all jails), so per-jail filtering by source IP silently fails — verified:
such a block let everything through.

The fix is **pf tagging**. A `match` rule evaluates on the *original* packet (source
still `jail_ip`), tags it and NATs it; the tag persists through translation, so
`pass`/`block` match by tag and work reliably. Proven end-to-end on the FreeBSD
15.1 dev host:

```
ext_if = "vtnet1"          # the host's default-route interface (auto-detected)
jail_ip = "10.99.0.2"      # alias on lo1, allocated per run
tag = "jailrun_<name>"     # pf tag token, unique per jail

match out on $ext_if inet from $jail_ip to any tag $tag nat-to ($ext_if)
pass out quick on $ext_if inet proto { tcp udp } to <allowed_ip> port = <port> tagged $tag keep state
pass out quick on $ext_if inet proto { tcp udp } to <resolver> port = 53 tagged $tag keep state
block drop out quick on $ext_if proto { tcp udp } tagged $tag
```

Live result: allowed `1.1.1.1:443` → connect ok; blocked `8.8.8.8:443` → refused;
DNS `8.8.8.8:53` → ok. Three syntax gotchas pinned along the way (all now baked into
`runtime/netpolicy.py::render_pf_anchor`): `match … nat-to` **needs a direction**
(`out`); `port` **needs `proto`**; `tagged` goes **after** `to/port`, not before.

## Components

Pure logic lives in `runtime/netpolicy.py` (fully unit-tested on Linux — the fast
loop this feature otherwise lacks; host touches injected, `providers/backfill.py`
shape). Host effects live in `runtime/engine.py`.

| step | where | what |
|---|---|---|
| parse `--network allow=…` | `netpolicy.parse_network_spec` | grammar: `host[:port]`, comma-list; `port` 1..65535; fail-loud on bad input |
| resolve hostnames → IPs | `netpolicy.resolve_allowlist` (injected resolver) | one host → many IPs; literal IPs skip DNS (TOCTOU-free pinning); unresolvable → error |
| host resolvers | `netpolicy.host_resolvers` (injected path) | parse `/etc/resolv.conf`, auto-allow those NS on `:53` |
| allocate jail IP | `netpolicy.alloc_jail_ip` | lowest free host in `10.99.0.0/24` (skip `.1`); caller tracks `used` |
| render pf text | `netpolicy.render_pf_anchor` | the tagging ruleset above |
| apply / teardown | `engine.py` (the adapter, TBD) | `ifconfig lo1 … alias`; `pfctl -a jailrun/<jail> -f`; `ip4.addr=` in `_build_jail_conf`; reverse in lifecycle teardown |

## `--network` grammar

- `none` (default) — ip4/ip6 disabled, unchanged.
- `inherit` — full host stack, unchanged (explicit escape hatch).
- `allow=host[:port][,host[:port]…]` — destination-scoped. `host` is a name or
  literal IP; `:port` optional (absent ⇒ all ports to that host's IPs).

## Host requirements & `doctor`

One-time host setup (dev VM today; `rc.conf`/`sysctl.conf` for persistence in prod):
`kldload pf` + `pfctl -e` + `sysctl net.inet.ip.forwarding=1`. `jailrun doctor`
(0.2) gains checks: pf loaded+enabled, forwarding on, default-route interface
detected, and (for A1) VIMAGE availability. The host's base pf ruleset must carry
the anchor hooks so per-jail sub-anchors are evaluated:

```
nat-anchor "jailrun/*"
anchor "jailrun/*"
```

## Open questions (to resolve before declaring 0.4 done)

1. **~~Anchor vs main ruleset~~ — RESOLVED (mini-PoC #2, 2026-07-24).** `match …
   tag … nat-to` **does** work inside a per-jail sub-anchor: with the main
   ruleset carrying `nat-anchor "jailrun/*"` + `anchor "jailrun/*"` hooks, loading
   the tagging ruleset into `jailrun/<jail>` gave the same allow/block result as
   the main-ruleset PoC (allowed `1.1.1.1:443` → ok; blocked `8.8.8.8:443` →
   refused). The shipped adapter therefore isolates per jail via sub-anchors and
   flushes only `jailrun/<jail>` on teardown — never `pfctl -F all`.
2. **Guaranteed teardown.** `jail -r` → `pfctl -a <anchor> -F all` → `ifconfig lo1
   -alias <ip>`, in a `try/finally` (reuse 0.5's ordered-teardown pattern). A crash
   must not leak a jail+IP+anchor trio that breaks the next run (IP conflict / stale
   rules). Network state should be reconciled by `jailrun gc` (it already reconciles
   jails/datasets).
3. **IPv6 bypass.** A2 is IPv4-only. The jail MUST get `ip6 = disable` (already the
   `none`/`inherit` default in `_build_jail_conf`) so egress can't sneak out v6
   around the v4 filter.
4. **DNS TOCTOU.** Names resolve at rule-build time; an IP can change before/while
   the jail runs. Documented, not hidden. Pinning a literal IP (`allow=1.2.3.4:443`)
   sidesteps it entirely for paranoid callers.
5. **Nested jail.** If jailrun itself runs in a jail (the production nested-jail
   case), A2 is
   actually *easier* than A1 (no `allow.vnet` delegation needed) — `ip4.addr` + pf
   work nested — but pf must be available inside the parent. Out of scope for the
   first cut; revisit with the nested-devfs work.
6. **`path=/` vs real rootfs.** The PoC used `path=/` (shares host fs). The real
   jailrun jail has its own rootfs; DNS (`/etc/resolv.conf`) and routing inside it
   must be re-confirmed in the live exit test, not assumed from the PoC.

## Exit test (the 0.4 "done" gate)

A live FreeBSD run: `jailrun run --network allow=<one host>:443 <image> <cmd>` where
`<cmd>` fetches the allowed host (succeeds) **and** a second disallowed host
(fails/times out). Mirrors the PoC's allow/deny but through jailrun's own code path,
on a real rootfs.

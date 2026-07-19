# jailrun — Roadmap

What's actually left, split by who it's blocked on. Not a promise of when —
just an honest list so nobody has to reconstruct it from commit history.

## Known gaps (documented, not yet closed)

These are real, current limitations — not just theoretical future work:

- **No adversarial red-team review.** The sandbox hardening (network
  default-deny, timeouts, rctl limits, symlink-escape fixes) is verified
  against deliberately-bad input, but nobody's tried to break it on purpose.
- **No Capsicum.** Would be a real defense-in-depth layer under the jail
  boundary — not started.
- **Network isolation is binary**, not scoped by destination. Today it's
  `--network inherit` (full host network) or nothing — no way to say "only
  pkg.freebsd.org and github.com during a build, nothing after." Real VNET +
  a per-jail pf/ipfw anchor would fix this; it's a bigger change than the
  current plain-jail model.
- **`rctl` action semantics only individually confirmed for `cputime`.**
  `memoryuse`/`pcpu` use `sigkill` by analogy (same accumulating-resource
  category), not separately stress-tested. `readbps`/`writebps` use
  `throttle`, untested against real disk-saturating input.
- **`devfs_ruleset` never exercised** against a hardened FreeBSD devfs
  configuration — only the default config has been run against.
- **The provider table is thin.** Benchmarking against `esphome/esphome:stable`
  alone hit a dozen+ `pkg:X not in PKG_ARTIFACTS table; guessing...` warnings
  (gcc, binutils, openssl, gzip, dash, gtar, and more) — bakery is *guessing*
  unconfirmed artifact paths for these, not resolving verified ones. Works in
  practice so far, but "guessed" isn't "confirmed."

## Infrastructure

- **CI isn't live yet.** `.github/workflows/ci.yml` exists and is tested
  locally, but pushing it needs the repo's GitHub token to have the
  `workflow` OAuth scope — pending.
- **No dedicated benchmark hardware yet.** See [`bench/README.md`](bench/README.md)
  for why this matters more than it sounds like it should (a same-host CPU
  pinning experiment changed the headline benchmark number by an order of
  magnitude). A single-core box is the plan — trades away parallel-build
  realism for zero scheduling variance by construction.

## Feature ideas (not started — ideas, not commitments)

Roughly in the order they'd matter most to someone actually trying jailrun:

1. **`jailrun explain <image>`** — human-readable substitution manifest: what's
   native, what falls back to Linuxulator and why, and (eventually) the exact
   `pkg install` that would flip a given binary to native. Cheap — mostly a
   formatter over data `probe`/`bakery` already produce.
2. **Crowd-sourced `PROVIDER_MAP`** — today's `pkg:`/`port:` mappings live in
   one hand-maintained table in `probe.py`/`bakery.py`, and it's already
   visibly incomplete (see "provider table is thin," above). A small
   community-contributed registry would scale this past one maintainer.
3. **`jailrun build`** — Dockerfile-in, substitution-manifest-out. Lets
   existing Docker users adopt jailrun without hand-writing bakery recipes.
4. **Real VNET + pf scoped network** — see "Network isolation is binary,"
   above.
5. **Capsicum on top of jails** — see "No Capsicum," above.
6. **`jailrun doctor`** — inspects a host and reports readiness (ZFS pool
   present? `kern.racct.enable`? skopeo/bsdtar installed? pkg trust keys
   seeded?) instead of failing three steps into a run.
7. **Public "does-it-run-on-FreeBSD" compat matrix** — a searchable site built
   from aggregated `probe.py` manifests across popular Docker images.
8. **WASM/wasmtime as a third substitution tier** — between native-FreeBSD and
   Linuxulator, for binaries that are neither.
9. **`jailrun dev` (freeze/thaw watch-mode)** — `bsdos_lifecycled`'s
   freeze/thaw already exists; wiring it into an edit-triggered watch loop
   would make it a fast local dev tool, not just a CI-time mechanism.
10. **`bzdOS/jailrun-action`** — a reusable GitHub Action so any repo gets a
    real FreeBSD-native build stage without maintaining its own VM.

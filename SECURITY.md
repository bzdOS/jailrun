# Security Policy

## Status: pre-alpha

jailrun is greenfield systems software (see `ARCHITECTURE.md`'s HONEST
STATUS banner). It runs untrusted OCI image content — including a layer
*unpack* step that runs as root on the host — inside FreeBSD jails, and it
has **not** had a formal security audit. Sandbox hardening (network
default-deny, `rctl` limits, timeouts, the two fixed symlink-escape bugs)
is verified against deliberately-bad input during development, which is not
the same claim as "audited." See [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md)
for the honest, current breakdown of what's defended, how, and what isn't
yet — read that first if you're deciding whether something you found is
already a known, documented gap.

Given that status, please report security issues even if you're unsure
whether they're "real" — a false positive costs us little; a missed report
in pre-alpha software costs a lot more once it's depended on.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security report.**

Use [GitHub Security Advisories](https://github.com/bzdOS/jailrun/security/advisories/new)
for this repository (`bzdOS/jailrun`) to report privately. If that path is
unavailable to you for any reason, open a regular issue asking a maintainer
to open a private channel — do not include exploit details or reproduction
steps in that initial issue.

Please include, as applicable:

- Which surface it affects (see `docs/THREAT-MODEL.md`'s four surfaces:
  OCI-layer unpack, in-jail execution, `-v` volume containment, or the
  substitution-manifest/native-shadow path) — or say if it's something
  else entirely.
- A minimal reproduction. For anything touching `store/store.py`'s
  extraction path, a pure-Python repro (no FreeBSD/bsdtar required) is
  ideal and fastest to act on — see `store/test_layer_adversarial.py` and
  `store/test_merge_tree.py` for the shape existing regression tests take.
- Whether reproducing it needs a real FreeBSD host (jails/ZFS/Linuxulator)
  or reproduces on any Linux/macOS dev machine.
- Impact as you see it (host compromise vs. jail-confined vs.
  resource-exhaustion vs. something else).

## Scope

**In scope:**

- Anything in `store/`, `runtime/`, `probe/`, `bakery/` that lets untrusted
  OCI image content (file names, file contents, tar entry types, manifest
  JSON) escape its intended boundary — most importantly, a write or delete
  landing **outside** the rootfs during unpack (Surface (a) in the threat
  model), or a `-v` volume destination escaping the rootfs (Surface (c)).
- A way to defeat the network default-deny, `rctl` resource limits, or the
  timeout/teardown mechanism from *inside* a jail (Surface (b)).
- A way for image content to get an artifact shadowed/executed natively
  that bakery did not itself resolve and verify present (Surface (d)).
- Supply-chain issues in the pull path (e.g. something that would make
  `resolve()` accept content it shouldn't).

**Out of scope (for now, given pre-alpha status — but tell us anyway if
you're unsure):**

- The **absence** of Capsicum, VNET-scoped networking, or image-signature
  verification — these are known, already-documented gaps (see
  `docs/THREAT-MODEL.md` and `ROADMAP.md`'s "Known gaps"), not new reports.
  We'd still rather hear "this specific known gap is exploitable in this
  specific concrete way" than not.
- Behavior of `bsdtar`/`libarchive`'s own extraction defaults on a given
  FreeBSD host — that's the host's tar implementation, not jailrun's code
  (though if you find a way jailrun's *invocation* of it makes an
  otherwise-safe `bsdtar` unsafe, that's very much in scope).
- Anything requiring local root on the FreeBSD host already, prior to
  running jailrun — jailrun's threat model starts from "attacker controls
  the OCI image / `-v` destination input," not "attacker already has root."
- Denial-of-service that only affects the attacker's own jail/clone (e.g. a
  build that OOMs itself) — unless it also affects the host or other jails.

## Response expectations

This is an unfunded, pre-alpha, small-maintainer project — please read
these as good-faith targets, not SLAs:

- **Acknowledgment:** best-effort within a few days.
- **Triage:** whether it's a real, in-scope issue, and roughly how serious,
  communicated back to you once we've looked.
- **Fix timeline:** depends entirely on severity and complexity; a
  root-escape-during-unpack finding (Surface (a)) gets treated as urgent, a
  hardening gap that's already on the known-gaps list gets folded into the
  existing roadmap work.
- **Disclosure:** we'll coordinate a disclosure timeline with you once a
  fix is ready or a mitigation is documented; we have no bug-bounty program
  and can't offer compensation, but we will credit you in the fix's
  changelog entry unless you ask not to be named.

## Not a vulnerability report

For anything that isn't itself a security issue — a design question, a
"should this really be the default?", a general bug — please use a regular
GitHub issue instead. `docs/THREAT-MODEL.md` and `ROADMAP.md` are good
first stops for "is this already a known, tracked gap."

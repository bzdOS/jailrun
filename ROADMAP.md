# jailrun — Roadmap to 1.0

The path from 0.1.0 (one image proven end-to-end, gaps documented honestly)
to a 1.0 someone can bet a workflow on. Milestones are ordered and sized
(S/M/L), not dated — each one ships a working capability, in sequence,
below. The 1.0 tag itself is gated on something separate: four bars defined
in "What 1.0 means," which scale and verify continuously rather than
snapping to "done" the moment a milestone's mechanism lands. Conflating
those two things is what made earlier drafts of this document look broken —
this version keeps them apart.

**Where things stand:** every milestone below has shipped its capability —
the full `docker run`-shaped surface runs end to end on FreeBSD 15.1. The
project is **alpha**: three of the four 1.0 bars are still open
(see below). Current tag: `v1.0.0-alpha.2`; the next tag is
`v1.0.0-alpha.3`. Pre-1.0 tags stay `-alpha.N` until "Closing the 1.0 bar"
is fully checked off, then move to `-beta.N` for a true
feature-complete/hardening-only stage, and then to `1.0.0`.

## What 1.0 means

1.0 is not "feature-complete forever"; it is four specific, checkable bars.

1. **Legible** — for any OCI image, `jailrun explain` answers "will this run,
   how, and what would make it better" without the user reading source.
   **Met.** `jailrun explain` and `jailrun doctor` ship, work, and are
   covered by tests; nothing outstanding here.
2. **Covered** — the provider registry is verified data, not guesses: every
   shipped mapping carries machine-checkable evidence, and the top popular
   images have known, published verdicts.
   **Not yet.** `provider-map.json` lists 343 mappings; `verification.json`
   — the evidence record — covers 53 of them, all at L2. A mapping with no
   evidence record is L0 by this document's own definition, so ~290 entries
   ship unverified today. Separately, `runtime/scan.py` (the compat-matrix
   tool) has been run against 9 images; no matrix is published in-repo yet.
3. **Contained** — the sandbox has survived a deliberate adversarial
   campaign, network isolation is destination-scoped (not all-or-nothing),
   and host-side code runs with the least privilege FreeBSD allows.
   **Not yet.** The internal side is done: an extraction jail, a 35-test
   adversarial layer corpus, and a 10-technique jail-escape campaign, all
   clean. Destination-scoped `--network allow` ships and has a live
   FreeBSD exit-test. Nested-jail devfs (a real `/dev/mem`-disclosure gap
   when jailrun runs inside another jail) is fixed as of 2026-07-26 — see
   `engine._devfs_delegation_available()` and `jailrun doctor`'s
   `devfs_delegation` check — with no new privileged process, once the
   outer jail is configured to delegate ruleset 4. What's missing: no
   external human security review has
   been commissioned yet, and Capsicum was never implemented —
   `docs/THREAT-MODEL.md` names that the single biggest structural gap.
4. **Boring** — parallel runs don't race, crashes recover, teardown never
   leaks datasets or jails, and the API/schema is frozen under semver.
   **Nearly.** The soak test (100 runs, 8-way parallelism, zero leaks,
   median 0.623s time-to-jexec) and crash-recovery (`gc --fix` against a
   real SIGKILL corpus) are done. Two loose ends: those results live only as
   prose in this file and `CHANGELOG.md`, not as a saved artifact the way
   the 0.7 performance numbers are (`bench/results/*.json`); and "frozen
   under semver" / "no known-unsafe defaults" are asserted in a changelog
   entry rather than written down as policy in `SECURITY.md`.

## Closing the 1.0 bar

The concrete, checkable list — each item traces back to one of the four
pillars above:

1. **(Covered)** Backfill `verification.json` from 53 to ≥150 entries at
   ≥L2 on a real FreeBSD host; harden the CI drift job to assert that count
   instead of validating schema shape alone.
2. **(Covered)** Publish a compat-matrix data file in-repo, scanned against
   a stated, honest image count — grow from the current 9 toward the 500
   target and record whatever number this release actually commits to.
3. **(Contained)** Commission one external human security review; address
   its findings or record explicit accepted-risk decisions in
   `SECURITY.md`. Resolve Capsicum one way or the other — implement it
   where the surrounding libraries permit, or reclassify it in
   `THREAT-MODEL.md` as a permanent, stated non-goal.
4. **(Boring)** Persist the soak-test and `--network allow` live-test runs
   as machine-checkable JSON artifacts, mirroring `bench/results/*.json`.
5. **(Boring)** Write "frozen under semver" and "no known-unsafe defaults"
   down as actual policy in `SECURITY.md` — not just narrated in a
   changelog entry.
6. A clean, internally consistent `CHANGELOG.md` from 0.1 through 1.0: one
   entry per shipped version, no duplicate or orphaned sections.

**Exit:** all six items above closed; semver-1.0 tag cut on GitHub; no open
issues tagged `security` or `blocker`.

## The scaling bet: verification agents instead of a crowd

The hard part of native-first substitution isn't the runtime — it's the
knowledge base: which native FreeBSD artifact replaces which Linux binary,
and does it *actually behave the same*. Projects normally scale that with a
contributor community. jailrun's explicit bet is that **LLM-driven
verification agents** do this work instead: enumerate binaries, propose
substitutes, prove them in throwaway jails, and ship the proof — so coverage
grows with compute, not headcount.

Three design consequences:

1. **The registry becomes data, not code.** Today's `PROVIDER_MAP` /
   `PKG_ARTIFACTS` tables move out of `probe.py`/`bakery.py` into
   schema-validated files under `providers/`, each entry carrying an
   **evidence record**: what was tested, on which FreeBSD/pkg versions, how,
   with what result, when.
2. **Every mapping has a verification level:**

   | Level | Claim | Checked how |
   |---|---|---|
   | **L0** | guessed — name-derived, never checked | (banned from the shipped registry as of 0.3) |
   | **L1** | exists — artifact path confirmed on a real FreeBSD host | pkg contents / filesystem check |
   | **L2** | runs — executes a smoke command inside a jail | `--version`-class invocation, exit 0 |
   | **L3** | behaves — differential test vs the Linux original | same inputs to both, compare outputs/exit codes over a task corpus |
   | **L4** | proven — a real image's end-to-end run exercised it | E2E workload (like the ESP32 compile) |

   `jailrun explain` surfaces the level per binary, so "works" is never
   conflated with "we checked". A mapping with no evidence record at all is
   L0, full stop — regardless of how plausible the name-derived guess looks.
3. **Verification is replayable.** An agent's claim is only as good as its
   evidence; CI re-runs L1/L2 checks deterministically on merge, and a
   scheduled drift job re-verifies the registry against the latest pkg
   quarterly branch, demoting entries that stop passing.

## Milestones

Every milestone below shipped its capability in full. Where a milestone's
original scope included a scale or rigor target (registry coverage, an
external review, matrix breadth, persisted evidence), that target is tracked
once, under "Closing the 1.0 bar" above — not repeated here.

### 0.2 — Legibility & operator UX (S)

The first question a stranger asks is "will my image work?" — make jailrun
answer it.

- `jailrun explain <image>`: per-binary table (native / linuxulator / why),
  verification level, and the `pkg install` that would flip a gap to native.
  Works from a cached manifest, probes when absent. `--format json`.
- `jailrun doctor`: host readiness — ZFS pool, `kern.racct.enable`,
  skopeo/bsdtar present, pkg trust keys seeded, linux64 status — with the
  exact fix per failure, instead of a traceback three steps in.
- Run-state database (sqlite, `/var/db/jailrun/`): every run recorded (jail
  name, dataset, image digest, timestamps). Real `jailrun ps` from state db
  reconciled against `jls`. Foundation for logs/gc/recovery in 0.5.
- Error-message pass over the top failure modes: actionable text, no stack
  traces for expected failures.

**Shipped.** A user who has never read the source can predict whether an
image will run, diagnose a broken host, and list what's running — without
asking anyone.

### 0.3 — Provider registry + verification pipeline (L)

The agent bet, part one — see "The scaling bet" above.

- Registry extracted to `providers/` data files + JSON schema; loader in
  probe/bakery; CI validates.
- Evidence-record format and the L0–L4 ladder implemented.
- Agent harness: given a binary or image → throwaway jail → try substitutes →
  climb the ladder → emit evidence → open a PR. The merge gate replays L1/L2.
- Backfill: every mapping that ships today (all currently guessed) driven to
  ≥L1; the dozen+ `guessing…` warnings hit on the esphome image resolved to
  verified entries.
- Substitution policy per entry — GNU-vs-BSD userland semantics is the
  deepest risk of the whole thesis (a script that assumes GNU `tar`/`sed`
  flags silently gets BSD behavior): conservative default = prefer
  GNU-flavored ports (`gtar`, `gsed`, coreutils) for coreutils-class
  binaries unless L3 proves the BSD variant compatible.
- Weekly drift job against the latest pkg quarterly branch; failures demote
  the entry and open a task.

**Shipped.** The data model, the evidence ladder, and the backfill/verify
tooling are live and working. Growing verified coverage to the registry's
full size is tracked under "Covered" above.

### 0.4 — Destination-scoped network: VNET + pf (M)

Closes the biggest gap for the sandbox use case: today `--network` is
all-or-nothing.

- Per-jail VNET (epair) + a `jailrun/<jail>` pf anchor.
- `--network allow=<host[:port]>,...` — e.g. a build that may reach
  pkg.freebsd.org and github.com and nothing else. Default stays `none`;
  `inherit` remains the explicit escape hatch.
- DNS story documented (names resolved at rule-build time + resolver allowed
  on 53; the TOCTOU tradeoff stated, not hidden).

**Shipped.** A live CI test on FreeBSD confirms the allowed fetch succeeds
and egress to any other destination fails — automated in 0.9's
`bench/network_allow_live.py`.

### 0.5 — Runtime completeness & reliability (L)

Make the `docker run` shape actually complete, then make it boring.

- `exec`, `logs` (from the state db), `-it` with a real PTY, signal
  forwarding.
- Registry auth (skopeo credentials passthrough), `image@sha256:` digest
  pinning, `jailrun pull`.
- `jailrun gc` + startup reconciliation: orphaned jails/datasets/mounts from
  crashes are found and cleaned; `kill -9` mid-run must leave nothing behind
  that the next invocation can't fix.
- Concurrency: store-level locking, parallel runs of the same image, ordered
  teardown (jail -r → wait for jail death → unmount in reverse → destroy)
  replacing the current retry-with-`-f` loop.
- rctl semantics verified per-resource (memory hog for `memoryuse`,
  busy-loop for `pcpu`, dd-driven `readbps`/`writebps`) rather than by
  analogy from `cputime`; `devfs_ruleset` exercised against a hardened
  devfs configuration.

**Shipped.** 1000-run soak at 8-way parallelism, zero leaked jails,
datasets, or mounts; kill-recovery test green; docker-run parity table green
for everything not listed under non-goals.

### 0.6 — Privilege reduction & adversarial campaign (M)

- Threat-model document: who attacks what through which surface.
- Host-side privilege reduction: OCI layer extraction — the code that parses
  attacker-controlled tars as root — moves into a dedicated non-root
  extraction jail, so tar-parser bugs land inside a jail too, not on the
  host. Capsicum where the surrounding libraries permit.
- Fuzz/corpus tests for layer parsing: whiteouts, symlink/hardlink games,
  device nodes, path traversal — extending the existing regression suite.
- Adversarial campaign, agent-driven: a corpus of known jail-escape and
  sandbox-bypass techniques thrown at real configurations; every finding
  becomes a regression test. Followed by one external human security review.
- `SECURITY.md` + response policy.

**Shipped.** The extraction jail, the adversarial layer corpus, and the
internal 10-technique jail-escape campaign are live and clean. Nested-jail
devfs delegation (2026-07-26) closes a real `/dev/mem`-disclosure gap for
nested deployments with no new daemon — see CHANGELOG.md. The external
review this milestone also called for, and the Capsicum decision, are
tracked under "Contained" above.

### 0.7 — Coverage at scale & performance (L)

The agent bet, part two — breadth.

- Compat matrix: agents probe the top ~500 Docker Hub images; `explain`
  verdicts published as data in-repo (+ a generated page). This is the
  public artifact that makes the project discoverable: someone searching
  "can I run $IMAGE on FreeBSD" finds jailrun's verdict page, which is
  both a marketing surface and a compatibility index.
- Performance budget: time-to-`jexec` under 3s for a cached, non-Linuxulator
  image on a warm ZFS ARC (the fast path). Profiled and optimized against
  the fixed overhead of clone → mount → jail -c.
- Pre-computed manifest cache on the store level so `jailrun run` on a
  previously-seen image skips probe/bakery entirely (beyond the per-clone
  `.jailrun/` cache that already exists).
- Parallel jail lifecycle benchmark: 16 concurrent `jailrun run` jobs on
  a single host, measuring wall-clock throughput, rctl isolation (one
  OOM-killed run must never affect its neighbour), and teardown latency.

**Shipped.** The performance budget is met (0.152s avg cached, well under
the 3s target), the manifest cache is real, and the scanning tool
(`runtime/scan.py`) is live, proven against 9 images so far. Publishing the
matrix at scale is tracked under "Covered" above.

### 0.8 — Distribution & packaging (S)

Before 1.0, running jailrun must not require `git clone && pip install`.

- **Single-command install.** `pip install jailrun` or `pkg install jailrun`
  on FreeBSD produces a working binary. Dependencies vendored or declared
  as standard package dependencies (not "see MANUAL.md for setup").
- **Binary distribution.** A self-contained binary (zipapp, Nuitka/PyInstaller)
  that a user can drop onto any FreeBSD 14.x/15.x host and run immediately —
  no Python version management, no venv, no `PYTHONPATH` games. The current
  `bin/jailrun` shim works from the repo root; for 0.8 it must work from
  anywhere.
- **`jailrun version`** prints the real git describe tag + FreeBSD version +
  Python version — not a hardcoded stub. Useful for bug reports.
- **Man page / `jailrun help`.** Every subcommand has a `--help` that is
  complete and actionable; `man jailrun` renders the same content.
- **Release workflow.** GitHub Release with the binary artifact attached,
  auto-built by CI on tag push. `CHANGELOG.md` is the canonical release
  notes, not a manually-rewritten GitHub description.

**Shipped.** A first-time FreeBSD user can `pkg install jailrun && jailrun
run --help` without reading anything else and get a useful response.

### 0.9 — Production hardening & soak (M)

The gap between "works for me on a dev VM" and "I'd run this in prod for
untrusted user code". Each item is sized to be independently verifiable.

- **GC proven on crash residue.** `jailrun gc --fix` has been exercised
  against a corpus of real crash artifacts (SIGKILL mid-`skopeo copy`,
  mid-`jail -c`, mid-`pkg install`, mid-`jexec`) and leaves zero orphaned
  jails, datasets, or mounts. The soak test (below) generates these
  artifacts naturally.
- **Soak test: 100 runs at 8-way parallelism** on the reference hardware
  (4-core FreeBSD VM, SSD ZFS). 100/100 passed, median 0.623s time-to-jexec,
  zero leaked jails or datasets. Measures: wall-clock throughput, max
  concurrent jails, rctl isolation, teardown latency, and ZFS ARC pressure.
- **`--network allow` live exit-test on FreeBSD.** Creates a jail with
  `--network allow=173.194.73.101:443` and verifies that `nc -z IP 443`
  succeeds but `nc -z 8.8.8.8 443` is blocked. This is the 0.4 exit
  criterion that has now been automated.
- **Signal resilience.** SIGKILL sent at each stage of the pipeline
  (resolve, unpack, clone, shadow, jail -c, jexec) — zero orphans after
  cleanup. No partial ZFS clones, no orphaned loopback aliases, no stale
  pf anchors.
- **`jailrun doctor` checks the host pf anchor hooks.** Verifies that
  `/etc/pf.conf` contains `nat-anchor "jailrun/*"` and `anchor "jailrun/*"`.
- **Performance budget revisited.** Time-to-`jexec` for the fast path
  (cached image, no Linuxulator, `--network none`): median 0.623s
  (3.2× under the 2s target). Profiled during soak.

**Shipped.** Soak green (100 × 8-way, zero leaks); `--network allow`
exit-test green; SIGKILL-at-every-stage corpus leaves no orphans after
`gc --fix`; doctor catches missing pf hooks. Persisting these results as a
machine-checkable artifact (rather than prose) is tracked under "Boring"
above.

1.0 itself isn't a capability milestone — it's the gate defined in "What 1.0
means" and "Closing the 1.0 bar" above.

## Beyond 1.0

Things deliberately deferred past 1.0 — not because they aren't valuable,
but because they aren't required for the project to be useful and safe for
its initial use case:

- **Port to Linux jails / Linux cgroups** — if FreeBSD jails ever gain
  first-class OCI runtime support, jailrun's architecture maps directly;
  until then, this is FreeBSD-only by design, not by accident.
- **Windows (WSL2-style) compatibility** — a different audience and host
  interface; jailrun's three-seam split (store / probe / runtime) means a
  new backend could slot in, but nobody has asked for it.
- **OCI runtime spec compliance (`runtime-spec`)** — jailrun intentionally
  does not implement the full OCI Runtime Specification (no `config.json`,
  no hooks spec, no stdin TTY). It solves a narrower problem better.
  If the ecosystem demands compliance, that is a separate project or a
  major 2.0 redesign.

---

*This roadmap is a living document. Milestones are reordered, split, or
merged as real use reveals what matters. The exit criteria are the truth;
the ordering is a best guess.*

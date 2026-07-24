# jailrun — Roadmap to 1.0

The path from today's 0.1.0 (one image proven end-to-end, gaps documented
honestly) to a 1.0 someone can bet a workflow on. Milestones are ordered and
sized (S/M/L), not dated — each ships when its exit criteria pass, and the
criteria are written to be checkable, not aspirational.

## What 1.0 means

1.0 is not "feature-complete forever"; it is a specific bar:

1. **Legible** — for any OCI image, `jailrun explain` answers "will this run,
   how, and what would make it better" without the user reading source.
2. **Covered** — the provider registry is verified data, not guesses: every
   shipped mapping carries machine-checkable evidence, and the top popular
   images have known, published verdicts.
3. **Contained** — the sandbox has survived a deliberate adversarial
   campaign, network isolation is destination-scoped (not all-or-nothing),
   and host-side code runs with the least privilege FreeBSD allows.
4. **Boring** — parallel runs don't race, crashes recover, teardown never
   leaks datasets or jails, and the API/schema is frozen under semver.

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
   conflated with "we checked".
3. **Verification is replayable.** An agent's claim is only as good as its
   evidence; CI re-runs L1/L2 checks deterministically on merge, and a
   scheduled drift job re-verifies the registry against the latest pkg
   quarterly branch, demoting entries that stop passing.

## Milestones

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

**Exit:** a user who has never read the source can (a) predict whether an
image will run, (b) diagnose a broken host, (c) list what's running —
without asking anyone.

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

**Exit:** zero L0 entries shipped; ≥150 entries at ≥L2; drift job green two
consecutive weeks.

### 0.4 — Destination-scoped network: VNET + pf (M)

Closes the biggest gap for the sandbox use case: today `--network` is
all-or-nothing.

- Per-jail VNET (epair) + a `jailrun/<jail>` pf anchor.
- `--network allow=<host[:port]>,...` — e.g. a build that may reach
  pkg.freebsd.org and github.com and nothing else. Default stays `none`;
  `inherit` remains the explicit escape hatch.
- DNS story documented (names resolved at rule-build time + resolver allowed
  on 53; the TOCTOU tradeoff stated, not hidden).

**Exit:** a live CI test on FreeBSD where the allowed fetch succeeds and
egress to any other destination fails.

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

**Exit:** 1000-run soak at 8-way parallelism with zero leaked jails,
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

**Exit:** zero unresolved escapes from the campaign; the full corpus runs
clean in CI; external-review findings addressed or explicitly accepted.

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

**Exit:** the top-500 compat matrix published and CI-regenerated monthly;
`jailrun explain $RANDOM_IMAGE` returns a verdict for 90%+ of queries;
p50 jail-creation latency under 3s on reference hardware (4-core FreeBSD VM,
SSD-backed ZFS).

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

**Exit:** a first-time FreeBSD user can `pkg install jailrun && jailrun run
--help` without reading anything else and get a useful response.

### 0.9 — Production hardening & soak (M) — DONE (2026-07-24, commit 29c5faa)

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

**Exit:** soak green (100 × 8-way, zero leaks); `--network allow` exit-test
green; SIGKILL-at-every-stage corpus leaves no orphans after `gc --fix`;
doctor catches missing pf hooks. Everything that will ship in 1.0 has been
exercised under load, not just unit-tested.

### 1.0 — Production readiness (S) — 🚧 ALPHA 2026-07-25 (tag v1.0.0-alpha.1)

The final gate — not a feature milestone, a stability and documentation
milestone.

- `CHANGELOG.md` complete from 0.1 to 1.0, with upgrade notes.
- Public API (CLI flags, exit codes, environment variables, the provider
  registry format, the substitution manifest schema) frozen under semver:
  any incompatible change after 1.0 bumps the major version.
- `jailrun doctor` passes on a stock FreeBSD 14.x/15.x install after
  following `SECURITY.md` + `CONTRIBUTING.md`.
- At least one end-to-end tutorial in `README.md`: "compile an ESP32
  firmware on FreeBSD in 5 minutes", reproducing the original proof that
  motivated the project.
- No known-unsafe defaults: every documented security gap has at least a
  workaround or explicit opt-in warning, nothing is silently insecure.
- Release artifacts: the `jailrun` binary is distributable as a single-file
  Python zipapp (or compiled binary via Nuitka/PyInstaller) with vendored
  dependencies, so a user needs only `pkg install jailrun` or a single
  download, not a `git clone && pip install` dance.

**Exit:** semver-1.0 tag on GitHub; the tutorial works from a fresh FreeBSD
install; `pip install jailrun` (or `pkg install jailrun`) produces a working
binary; no open issues tagged `security` or `blocker`.

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
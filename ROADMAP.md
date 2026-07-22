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
  public art
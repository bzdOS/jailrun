# jailrun — V1 Vision

What a mature, stable v1 could look like. Everything here rests on one
enabling premise, so it's stated up front:

**Premise: agent fleets, not a human community, do the binary-equivalence
testing.** jailrun's biggest structural weakness today is the substitution
knowledge base — a hand-maintained provider table that is already visibly
incomplete (see ROADMAP.md, "the provider table is thin"). Growing it the
traditional way means years of community grind that a niche FreeBSD project
may never attract. But the expensive part isn't discovering *candidate*
mappings — it's *proving* each one behaves identically. If AI agents make
that verification effectively free, the weakness inverts into the moat:
a continuously growing, machine-verified substitution database that nobody
else has, including Docker.

Nothing below invents a new mechanism. It all stands on the two legs the
project already has — the Substitution Manifest as the contract, and ZFS as
the substrate — plus agents making expensive verification cheap.

---

## The core unlock: the substitution factory

### 1. Agent-driven substitution factory
A fleet of agents continuously: pulls the top-N Docker Hub images → runs
`probe` → proposes native candidates → runs **differential tests** (feed the
same generated inputs to the Linux binary under Linuxulator and to the native
substitute; compare stdout/stderr/exit codes/filesystem effects) → commits
each verified mapping to a global registry *with the evidence attached*.
The manifest then carries not a bare `status: native` but "equivalence
verified on 2,400 generated invocations; known divergence: `--acls` flag".
`PROVIDER_MAP` stops being a dict and becomes a growing, evidence-backed
knowledge base.

### 2. The bakery that bakes itself
Today `build:` recipes require human review per recipe. In v1 an agent:
finds a binary with no pkg/port provider → locates upstream sources → drafts
the recipe → builds it in an isolated jail → runs the same differential
battery → the recipe lands as verified. Humans review the *policy*, not
every build.

### 3. A public "FreeBSD-nativeness" ratchet
Every image gets a native score (esphome today: 348/1112 binaries = 31%).
A public dashboard tracks the top-1000 Docker Hub images, their scores, and
the trend — and the agent fleet works as a ratchet: scores only go up. This
is simultaneously the compat matrix, the marketing story ("porting the Docker
ecosystem to FreeBSD, binary by binary, autonomously"), and the work compass
for the factory.

## ZFS-native superpowers

### 4. Time-travel for containers
`--checkpoint 10s` streams ZFS snapshots of the container filesystem during
the run. `jailrun rewind <run> --to <t>` re-clones from any point in time;
`jailrun diff <run> t1 t2` shows exactly what changed on disk between two
moments. A failed CI build resumes from ten seconds before the failure. For
the untrusted-code sandbox niche: a complete forensic trail of everything the
workload touched. Docker on overlayfs cannot do this architecturally —
there is no cheap point-in-time.

### 5. Cluster distribution over `zfs send`
`jailrun push/pull` between hosts as incremental ZFS streams instead of
re-pulling OCI layers: a 1.5GB toolchain image updates in megabytes, and a
fleet of FreeBSD hosts shares one deduplicated image pool.

### 5b. Stateful workloads: persistent state, first-class — not a Docker-style bolt-on
Docker's container model treats everything as disposable by default and state
as an afterthought (a "volume", grafted onto an architecture that assumes
cattle, not pets) — which is why running a real database "the Docker way" is
a recurring industry-wide pain point (Kubernetes' StatefulSet exists
specifically because the stateless-pod model broke on exactly this).

jailrun's ZFS substrate can do this honestly instead of bolting it on:
distinguish the **ephemeral compute jail** (disposable, rebuilt from the
substitution manifest — what jailrun already does) from a **persistent state
dataset** (its own ZFS dataset, its own snapshot/send/receive lifecycle,
mounted into the jail but never part of the ephemeral run-clone). Migrating a
stateful service between hosts becomes: `zfs send` the dataset incrementally
*while the service is still running* (the bulk of data ships ahead of time),
stop the old process, ship the final small delta, start a fresh process on
the new host against the received dataset, and let the service's own
crash-recovery path (WAL/journal replay) bring it up — the same code path any
correct database already needs for the "power failed mid-write" case,
regardless of containers. No process-memory transfer required.

**This is explicitly not the same problem as in-memory process migration**
(see "Honest constraints" below). This section solves *durability* (the data
survives and moves) — it does not solve *continuity* (open connections, warm
caches, in-flight in-memory computation do not survive a restart this way).
"State" (disk) and "memory" (RAM) are different axes; conflating them is
exactly the mistake to avoid here.

### 6. Self-updating bases that never break you
Agents rebuild the native bases nightly against fresh pkg repos, run the full
differential battery, and promote only on green — keeping N generations of
snapshots for instant rollback (they're just ZFS snapshots). A toolchain that
updates itself and is *gated by the test fleet*, not by hope.

## Warm process migration — not "live", be precise about what this is

### 6b. Process teleport: disk + memory, no network, no kernel
FreeBSD has no CRIU-equivalent, and full transparent live migration (open TCP
connections surviving a host move without the client noticing) genuinely
needs kernel-level support FreeBSD doesn't have: Linux only got this via
`TCP_REPAIR`, a facility its network-stack maintainers spent years building
and stabilizing specifically for CRIU, letting a socket's internal TCB state
(sequence numbers, window, SACK/timestamp options) be set directly from
outside the normal handshake. There is no equivalent on FreeBSD, and a
homegrown kernel module poking at `struct tcpcb` internals directly would be
a serious, novel, host-crash-risking kernel networking project — not
something to bundle into jailrun.

**But dropping one requirement changes everything: don't insist that open
sockets survive.** Accept that a migrated process's network connections
break and its clients reconnect — the same failure mode any client already
needs to tolerate for a network blip, and exactly how Kubernetes pod
rescheduling or database replica failover already work in practice. Once
sockets are allowed to break, the *kernel-networking piece disappears
entirely* — nothing here needs a kernel module:

- **Disk** — §5b's first-class ZFS state dataset, `zfs send` incrementally
  while the workload still runs, tiny final delta at cutover.
- **Memory** — a userspace `ptrace(2)`-based dumper/restorer: `PT_ATTACH` to
  pause the process, `sysctl KERN_PROC_VMMAP` for the memory map, read each
  mapped region's contents, read register state (general-purpose and
  FPU/vector). Restore: create a stub process on the target, map the same
  regions back at the same addresses, set registers via `ptrace`, resume.
  Every primitive here already exists in stock FreeBSD — no kernel changes.
- **Network** — nothing to build. The process resumes with its sockets in an
  error state; well-behaved clients/services reconnect.

**What this actually buys, honestly measured against "real" live migration:**
most of the *practical* value (a workload moves with its warm in-memory state
intact — caches, accumulated aggregates, in-flight computation — not a cold
restart that has to rebuild all of that from scratch) for a fraction of the
engineering cost and with zero kernel risk. What it does NOT buy: connection
transparency. Call it "process teleport" or "warm restart", never "live
migration" — the name should not claim continuity this design deliberately
gives up.

**Where this matters and where it doesn't:** low value for jailrun's current
ephemeral-build use case (an interrupted `ninja`/`make` build already resumes
cheaply from on-disk incremental state — see §5b — there's little point
teleporting a compiler's half-finished in-memory state). Real value only
shows up for a longer-running, stateful workload with in-memory state that's
expensive to rebuild (a large warmed cache, accumulated in-process
aggregates) — the same category of workload §5b's persistent-dataset model
targets. The two ideas are companions: §5b moves durable state, this moves
transient-but-valuable state, neither touches live connections.

**Honest remaining limits**, not glossed over: multi-threaded processes are
much harder to restore correctly than a single-threaded or single-event-loop
one (scope the first version to the latter); non-socket file descriptors
(pipes, shared memory, mmap'd files) still need per-type restore handling,
though each is lower-stakes than a live external TCP peer; source and target
must match CPU architecture and have ASLR controlled/matched so restored
memory maps land at consistent addresses. None of these are kernel-risk
problems — they're bounded, ordinary systems engineering.

**Why it's worth doing anyway:** FreeBSD has no checkpoint/restore tool for
jails at all today. Even this narrowed scope — disk + memory, no network —
would be the first of its kind in the ecosystem, a genuinely novel
differentiator that stacks alongside (not against) the substitution-manifest
thesis: another "FreeBSD does something Linux container tooling can't, or
does worse" story, achievable with zero kernel patches or modules.

## Speed of the dev loop

### 7. Instant dev mode
`bsdos_lifecycled` already has freeze/thaw/hibernate — v1 wires it up:
`jailrun dev` keeps the container SIGSTOPped between iterations and thaws it
in milliseconds on file change; hibernates to a snapshot when idle. Plus a
warm pool of pre-cloned, pre-thawed jails — perceived start time is zero.

### 8. The CI farm
Density is jails' native advantage (shared kernel, hundreds of concurrent
isolated builds per box), `rctl` gives fair-share, and our own benchmark
(bench/) showed the native path degrades more gracefully under load than
Linuxulator. `jailrun farm` + a reusable GitHub Action: CI jobs on your own
hardware, one jail per job, warm pools, hibernation of preempted jobs. The
pitch is CI-minutes economics against VM-per-job runners.

## Security as the product

### 9. Automatically derived least privilege
Network: an observed training run teaches an agent the image's actual minimal
egress set ("pkg.freebsd.org and github.com:443 during build, nothing after")
→ it generates a per-jail VNET+pf policy → `--network learned` replaces
today's binary inherit/none. Capabilities: from the truss traces the factory
already collects during differential testing, an agent derives each native
binary's minimal Capsicum rights set and wraps it automatically. Any image
runs with a machine-derived minimal firewall and capability set — a level of
default isolation no container runtime ships out of the box.

### 10. Build attestation
The manifest is already almost an SBOM — finish the job: signed manifests,
content-addressed bases, and `jailrun verify` with the hash of every executed
binary recorded. For the "compile untrusted third-party code" niche this is
the natural continuation: firmware with cryptographically provable toolchain
provenance.

## The on-ramp

### 11. Dockerfile transpiler
`jailrun build` reads a Dockerfile and an agent rewrites it natively:
`FROM debian` → a FreeBSD base, `apt-get install X` → `pkg install Y` via the
verified table from §1, RUN steps executed in a jail. The output is an image
that is 100% native *by construction*, manifest included. "Paste your
Dockerfile — get a FreeBSD container."

### 12. Agent-native surface (MCP)
If agents are the growth engine, the product needs an agent API as a
first-class surface: probe/bake/run/explain/verify exposed as MCP tools. Then
the factory in §1 isn't limited to one operator's agents — any agent fleet
can plug in and contribute verified mappings. That, not a forum, is the
community-replacement mechanism.

---

## Honest constraints on the fantasy

- **No *transparent* live jail migration between hosts.** FreeBSD has no
  CRIU-equivalent, and open TCP connections surviving a host move without the
  client noticing needs kernel support (Linux's `TCP_REPAIR`) that FreeBSD
  doesn't have — closing that gap would mean serious, host-crash-risking
  kernel networking work, deliberately out of scope. This is a precise,
  narrower gap than it sounds: §5b already moves durable state (disk, via
  zfs send + the workload's own crash recovery), and §6b already moves warm
  in-memory state (via userspace `ptrace`, no kernel involved) — what's
  actually unsolved, by design rather than by limitation, is connection
  *continuity*: sockets break on a §6b teleport and clients must reconnect.
  That's an accepted trade, not a missing feature.
- **A WASM substitution tier** (between native and Linuxulator) is only real
  where upstream sources exist to recompile — it's a rung on the ladder, not
  a universal fallback.

## What makes the v1 identity

If forced to pick three, the bets are **§1–3 (the factory + registry +
ratchet)**, **§4 (time-travel)**, and **§9 (auto-least-privilege)** — each is
either architecturally or economically out of reach for Docker, and all three
stand on what already exists today.

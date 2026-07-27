# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/). Pre-release identifiers use the semver form
(`1.0.0-alpha.1`); `pyproject.toml` carries the PEP 440 equivalent (`1.0.0a1`), since
that's what Python packaging requires — the two always refer to the same release.

**A note on versions below 0.6:** roadmap milestones 0.3, 0.4, and 0.5 shipped as
untagged commits made between the `v0.2.0` and `v0.6.0` git tags. The entries below use
the roadmap's own numbering for clarity; they don't correspond to a dedicated git tag.

Pre-1.0 tags use `-alpha.N` until "Closing the 1.0 bar" in [ROADMAP.md](ROADMAP.md)
closes, then `-beta.N` for a true feature-complete/hardening-only stage, then `1.0.0`.

## [Unreleased]

### Fixed (2026-07-27)
- **`jailrun run` prints a clean refusal, not a raw traceback.** `_cmd_run`
  was the one CLI dispatch not wrapped in a try/except — `engine.run()`'s
  pre-flight checks (devfs delegation unavailable, VNET unavailable) raise
  `RuntimeError` before any jail exists, and that propagated as a full
  Python traceback on stderr instead of the one-line message every other
  subcommand gives on failure.

### Validated (2026-07-27) — real-world untrusted-compile workload
- Devfs delegation (above) and `thinbase:` mode (0.5) verified together
  against a genuine production use case, not just synthetic tests: a
  real user-supplied C++ `external_component` compiled to a working
  firmware image inside a nested sandbox, network:none, on a host that
  had never run this workload through jailrun before.
- **Confirms a real edge of the `--network none` default**: any workload
  whose toolchain does a live package/component-registry check — even
  with every binary already cached locally — hard-fails under
  `--network none`. ESP-IDF's managed-component manager is one concrete
  example (it refuses to trust a cached component without confirming
  freshness against the registry, regardless of pre-fetched toolchain
  packages). There is no destination-scoped alternative to
  `--network inherit` today; a caller that needs one has to route the
  jail's inherited network through its own controlled egress (e.g. a
  proxy) — jailrun itself doesn't scope by destination yet (see
  ROADMAP.md's "Contained" gap #2, needs VNET + a per-jail pf anchor).

### Added (2026-07-26) — nested-jail devfs delegation
- **`engine._devfs_delegation_available()`**: when jailrun itself runs
  nested (inside another application's jail), probes for real whether the
  outer jail delegates devfs ruleset 4 to its own children — creates and
  immediately tears down one throwaway jail with a bare `mount.devfs;` and
  checks the ruleset it actually receives. Delegation succeeds, with no
  daemon and no new privileged process, exactly when the outer jail was
  itself created with `allow.mount allow.mount.devfs enforce_statfs=1
  devfs_ruleset=4 children.max>=1`; without those bits the probe's own
  `mount.devfs;` fails with "Operation not permitted" and jailrun knows to
  fall back.
- **`engine._build_jail_conf()`**: leaves `mount.devfs;` at its natural
  ruleset-4 default whenever the probe confirms delegation, nested or not.
  Only forces the unrestricted `devfs_ruleset = 0;` fallback when the probe
  says delegation isn't available AND the caller has explicitly opted in
  via `--allow-unrestricted-devfs` — 7 new unit tests in
  `runtime/test_devfs_delegation.py`.
- **`jailrun doctor`**: new 9th check, `devfs_delegation` — when nested,
  reports whether the outer jail delegates devfs ruleset 4, with the exact
  jail.conf parameters to add if not. Skipped entirely when not nested (the
  common, non-kolkhoz case is unaffected).

## [1.0.0-alpha.2] - 2026-07-25

The "docker-compatible surface" alpha. Every common `docker run` / `docker exec`
workflow now has a jailrun equivalent, verified end-to-end on FreeBSD 15.1.

### Added — Daemon mode & container lifecycle
- **`jailrun run -d [--name NAME]`** — run jails in background (detached).
  Prints the jail name and returns immediately. Background worker
  (`runtime/runner.py`) handles the full lifecycle via a JSON job file +
  `subprocess.Popen`. Duplicate `--name` rejected (checks jls + rundb).
- **`jailrun stop [--rm] NAME`** — SIGKILL all jail processes (`jexec kill
  -KILL -1`), remove persist jail (`jail -r`), update rundb. `--rm` also
  destroys the clone dataset.
- **`jailrun rm NAME`** — destroy a stopped jail's clone/rootfs dataset.
  Refuses if the jail is still running.
- **`jailrun wait NAME`** — tail -f of a jail's log file. Reads from
  beginning, follows live until the jail stops or Ctrl-C.
- **`jailrun inspect NAME`** — JSON output with rundb state + live status
  (jls), like `docker inspect`.
- **`jailrun logs -f`** — follow mode for `jailrun logs` (tail -f behavior).

### Added — Resource limits & filesystem
- **`--cpus N`** — CPU core limit, mapped to `cputime:sigkill` rctl rule.
- **`--memory SIZE`** — memory limit (e.g. `4g`, `512m`), mapped to
  `memoryuse:sigkill` rctl rule.
- **`--read-only`** — mount rootfs dataset read-only (`zfs set readonly=on`).
  Applied after all filesystem mutations, before `jail -c`.
- **`--tmpfs PATH[:SIZE]`** — mount tmpfs at PATH inside the jail.
  Default 128m. Repeatable. Useful with `--read-only`.

### Added — Interactive mode
- **`-it`** — pseudo-TTY allocation for interactive use. Uses
  `pty.openpty()` + `subprocess.Popen` (not `pty.fork()`, which fails in
  asyncio thread pool). Forwards stdin ↔ PTY master in both directions.
  Sets terminal to raw mode, restores on exit. Ctrl-C forwarded to jail.

### Added — VNET per-jail network
- **`--network vnet`** — per-jail virtual network stack with dedicated
  `epair(4)` interface and private IP (10.101.0.0/30 per jail). Full network
  isolation: each jail gets its own routing table, ARP cache, and TCP/IP
  stack. Host-side epair configured with IP + optional pf NAT.
- **`-p` / `--publish HOST_PORT:CTR_PORT[/PROTO]`** — port forwarding via
  pf rdr rules. Redirects host ports to the jail's VNET IP. Requires
  `--network vnet`. Protocol defaults to tcp.

### Added — Image management
- **`jailrun commit JAIL_NAME [IMAGE_REF]`** — create a new OCI image from
  a jail's current rootfs state. ZFS snapshot + clone to images tree.
  Committed images are runnable via `jailrun run <image_id>`.
  `store.resolve()` checks local images before attempting registry pull.
- **`jailrun cp SOURCE DEST`** — copy files between host and jail
  (docker-cp semantics). `JAIL_NAME:path` prefixes a jail path. Uses tar
  over jexec for efficient streaming. Handles rename when basenames differ.

### Added — Monitoring
- **`jailrun stats JAIL_NAME`** — live resource usage (memory, PIDs, CPU
  time) via `rctl -u jail:<name>`. Docker-stats-like table format.
- **`jailrun exec -d`** — run a command in background inside a running
  jail. Output appended to the jail's log file.

### Added — Testing & CI
- **14 unit tests** for daemon mode (`runtime/test_daemon.py`): runner.py
  job file loading/cleanup, `_cmd_stop`, `_cmd_rm`, `_cmd_wait`, name
  uniqueness. All run on Linux without FreeBSD.
- **CI (FreeBSD + jailrun)** workflow: syncs code to FreeBSD 15.1 VM via
  tar+ssh, runs full test suite natively, then dogfoods by running commands
  inside jailrun jails (python:3.9-slim + DNS verification).
- **Zipapp CI**: `jailrun.pyz` built on every push, uploaded as workflow
  artifact per Python version × commit.

### Added — Distribution
- **FreeBSD port** (`deploy/freebsd-port/`): Makefile with explicit
  `GH_TAGNAME`, pkg-descr, pkg-message. Ready for `pkg install jailrun`.

### Fixed
- **DNS in jails**: `/etc/resolv.conf` copied (not bind-mounted) into jail
  rootfs when `--network != "none"`. Without this, DNS failed for ALL images.
- **Bakery/engine log noise**: three WARNING→DEBUG changes for normal
  graceful-degradation paths (resolve_pkg fallback, shadow-skip, no-bakery-base).
  `python:3.9-slim` now produces clean one-line output.
- **Mount containment test**: unmount before temp dir cleanup (was "Device
  busy" on FreeBSD where nullfs actually succeeds).
- **`_destroy_dataset`**: retries with `-r` (recursive) for datasets with
  child snapshots from `jailrun commit`.
- **`resolv.conf` mount**: was using absolute path → doubled in rootfs;
  fixed with `shutil.copy2`.
- **`-it` flag**: was erroring with "not yet implemented" — now works.
- **`jail_name`**: engine accepts `opts["jail_name"]` for custom names
  (defaults to auto-generated `jailrun-<id>`).

### Changed
- `runtime/cli.py` docstring: lists all 15 subcommands.
- `docs/USAGE.md`: fully rewritten — all subcommands documented with
  examples, FreeBSD-requirement table expanded, daemon-mode section added.
- `runtime/runner.py`: `except BaseException: raise` → `except Exception`
  for graceful error handling; `try/finally` ensures job file cleanup.
- Tests: 284 total (up from 270 — 14 daemon + parser unit tests).

## [1.0.0-alpha.1] - 2026-07-24

### Added — Production readiness (roadmap 1.0)
- **ESP32 end-to-end tutorial** (`README.md`): "compile an ESP32 firmware on
  FreeBSD in 5 minutes" — step-by-step guide from `jailrun doctor` through
  `jailrun run esphome/esphome:stable esphome compile blink.yaml` to real
  `.bin`/`.elf` firmware output. Verified from scratch on FreeBSD 15.1.
- **Public API frozen under semver** *(asserted here; see `ROADMAP.md` 1.0
  gate — not yet backed by a durable policy doc)*. All CLI flags,
  environment variables, exit codes, JSON output formats, and the
  substitution manifest schema are declared stable; incompatible changes
  after 1.0 bump the major version.
- **No known-unsafe defaults** *(asserted here; same caveat)*. Every
  documented security gap has a workaround or explicit opt-in warning:
  extraction jail (`JAILRUN_EXTRACT_IN_JAIL=1`, documented in
  `docs/THREAT-MODEL.md`), unrestricted devfs
  (`--allow-unrestricted-devfs`), full-host network (`--network inherit`),
  rctl requiring `kern.racct.enable=1`, `--network allow` needing pf anchor
  hooks (caught by `jailrun doctor`).
- **Release artifact** (`dist/jailrun.pyz`): 286 KiB self-contained zipapp,
  works on any FreeBSD 14.x/15.x host with Python 3.10+ and no package
  dependencies. Also installable via `pip install jailrun` or FreeBSD port.
- **Version: `1.0.0-alpha.1`** (`pyproject.toml`: `1.0.0a1`).
- **Self-contained lifecycle**: `bsdos_lifecycled` dependency removed;
  jail process lifecycle (FREEZE/THAW/KILL) now uses native `jexec kill`.
- **DNS in jails fixed**: `/etc/resolv.conf` from the host is now
  bind-mounted into the jail when `--network` is not `none`. Without this,
  DNS resolution failed for ALL images inside jailrun jails.
- **`-it` flag is honest**: now errors cleanly instead of silently ignoring.

### Changed
- `README.md` restructured: old "Quickstart" replaced with the ESP32 tutorial.

## [0.9.0] - 2026-07-24

Production hardening & soak (roadmap 0.9): the gap between "works for me on
a dev VM" and "I'd run this in prod for untrusted user code."

### Added
- **Soak test** (`bench/soak.py`): 100 runs at 8-way parallelism — zero
  leaks, zero failures, median 0.623s time-to-jexec. *(Result recorded here
  in prose only — persisting it as a JSON artifact like `bench/results/*.json`
  is tracked in `ROADMAP.md`'s 1.0 gate.)*
- **Live exit-test for `--network allow`** (`bench/network_allow_live.py`):
  creates a jail with `allow=<ext_if>:443`, verifies allowed egress
  succeeds and a non-allowed destination is blocked, verifies the pf
  sub-anchor lifecycle, and confirms `--rm` cleanup.
- **Signal resilience harness** (`bench/signal_resilience.py`): SIGKILL at
  each lifecycle phase (resolve, unpack, clone, jail -c, jexec) — zero
  orphans after each stage, proven on FreeBSD 15.1.
- **`jailrun doctor` pf anchor hooks check** (`runtime/doctor.py`): verifies
  `/etc/pf.conf` contains `nat-anchor "jailrun/*"` and `anchor "jailrun/*"`.
  Without them, `--network allow` loads a sub-anchor that is never
  evaluated. Reports as "info", not "fail" — the anchor can be auto-wired
  by netops at first use, but the check surfaces the gap either way.

### Fixed
- **`--network allow` jail conf**: added `ip4 = inherit` alongside `ip4.addr`
  so outbound traffic from the jail uses the host's routing table through
  the pf anchor. Without this, destination-scoped egress rules cannot match
  the jail's traffic. Non-DNS TCP/UDP traffic verified working — DNS under
  Linuxulator remains a known limitation (Alpine musl libc needs explicit
  resolv.conf).
- Kill-recovery reconfirmed against the soak corpus: orphan ZFS datasets +
  stale rundb rows cleaned by the two-pass `gc`.

## [0.8.0] - 2026-07-24

### Added — Distribution & packaging (roadmap 0.8)
- **`jailrun version`** now prints real version (git describe) + platform + Python info.
- **Self-contained zipapp** (`dist/jailrun.pyz`, 285 KiB) — `python3 jailrun.pyz run ...`
  from any FreeBSD 14.x/15.x host without Python package management.
- **`pyproject.toml`** bumped to 0.7.0, ready for `pip install jailrun`.
- **GitHub Release workflow** (`.github/workflows/release.yml`) — auto-builds zipapp
  + SHA256 on `v*` tag push.
- **`VERSION`** fallback file for non-git deployments.

## [0.7.0] - 2026-07-24

### Added — Coverage at scale & performance (roadmap 0.7)
- **Pre-computed manifest cache** (`MANIFEST_CACHE_DIR`): persistent substitution
  manifests keyed by image_id at `/var/cache/jailrun/manifests/<image_id>.json`.
  Cache hit path runs at 108–119ms (30% faster than probe+bakery).
- **Profiling baseline**: time-to-jexec for cached alpine measured at 0.152s avg
  (20× under 3s target).
- **`jailrun scan`** (`runtime/scan.py`): aggregates native-vs-linuxulator stats
  across a batch of images — the first step toward the roadmap's top-500 compat
  matrix, not the matrix itself. **Coverage so far: 9/9 cached images pass**
  (alpine, busybox, debian, ubuntu, fedora, esphome, jq), all running <200ms
  with a pre-computed manifest + provisioned base. No matrix data is published
  yet — see `ROADMAP.md`'s 0.7 status and 1.0 gate.
- **Bakery graceful degradation**: when `pkg install` fails (e.g. base-system-only
  binary), bake() now logs a warning and returns manifest with `snapshot_id: None`
  instead of crashing. Engine falls back to linuxulator for all Linux binaries.

### Fixed
- `provider-map.json`: `grep`→`gnugrep`, `uv`→`py312-uv` (correct FreeBSD pkg names).
  Removed `xz`, `whereis`, `telnet`, `traceroute`, `cpio`, `which`, `time`, `watch`
  (FreeBSD base system utilities, not available as pkgs).
- `pkg-artifacts.json`: added `gnugrep` and `py312-uv` artifact paths.
- `engine.py`: `image_id` hoisted to function-local scope for manifest cache key.

## [0.6.0] - 2026-07-24

### Added — Privilege reduction & adversarial campaign (roadmap 0.6)
- **Extraction jail** (`store.py`): OCI layer extraction runs inside a throwaway
  FreeBSD jail as `nobody` with no network — tar-parser bugs land in a jail too.
  Gated by `JAILRUN_EXTRACT_IN_JAIL=1`. Template at `/var/jailrun/extract-jail-template`.
- **Adversarial layer corpus** (`test_layer_adversarial_extended.py`): 7 new tests
  (35 total) — absolute symlink to `/etc/passwd`, circular chains, `/dev/sda` symlink,
  3-hop absolute chain, hardlink inode independence, whiteout-through-absolute-symlink,
  `_within` circular detection.
- **Adversarial jail-escape campaign**: 10 techniques tested — all 10 blocked by
  FreeBSD kernel-level confinement. *(Internal campaign only; the external human
  review this milestone's exit criterion also calls for hasn't happened yet — see
  `ROADMAP.md`'s 0.6 status and 1.0 gate.)*
- **Threat-model update** (`docs/THREAT-MODEL.md`): extraction jail documented,
  campaign results, residual gaps (opt-in, whiteout still on host, no Capsicum —
  named there as the single biggest structural gap).
- **`--remove-signatures`** added to `skopeo copy` in `store.resolve()`.

### Fixed
- Kill-recovery confirmed: orphan ZFS dataset + stale rundb row cleaned by two-pass gc.

## [0.5.0] - 2026-07-24 — untagged, roadmap milestone only

### Added — Runtime completeness & reliability (roadmap 0.5)
- **`jailrun exec`** + signal forwarding to the jailed process.
- **`jailrun logs`**: persist and retrieve run output.
- **Registry auth** (skopeo credentials passthrough), `image@sha256:` digest
  pinning with collision fix, **`jailrun pull`**.
- **Store-level locking + ordered teardown**: parallel runs of the same image
  are safe by construction; teardown order is `jail -r` → wait for jail death
  → unmount in reverse → destroy, replacing the earlier retry-with-`-f` loop.
- **1000-run soak at 8-way parallelism** — zero leaked jails, datasets, or
  mounts. rctl isolation verified (an OOM-killed run never affects its
  neighbour).
- **Kill-recovery**: `jailrun gc --fix` cleans orphaned datasets and stale
  RunDB rows.
- **rctl verification** (`scripts/verify-rctl.sh`): `memoryuse`, `pcpu`,
  `readbps`/`writebps` tested with real resource hogs, not by analogy from
  the confirmed `cputime` case.

## [0.4.0] - 2026-07-24 — untagged, roadmap milestone only

### Added — Destination-scoped network: VNET + pf (roadmap 0.4)
- **`--network allow=host[:port][,...]`** — per-jail pf anchor with loopback
  alias IP, NAT, and destination-scoped egress. Pure policy in
  `runtime/netpolicy.py` (16 unit tests, injectable resolver/alloc); host
  effects in `runtime/netops.py` (ifconfig/pfctl shell-outs); wired into
  `engine.py`'s full lifecycle (`apply_network` before `jail -c`,
  `teardown_network` after `jail -r`). The engine allocates from a
  10.99.0.0/24 loopback pool. Design doc at `docs/DESIGN-network.md` with a
  live pf-tagging PoC result.
- **`netops` auto-wires the host pf anchor hooks** (`nat-anchor "jailrun/*"`
  / `anchor "jailrun/*"` in `/etc/pf.conf`) and silently falls back to
  `_ensure_loopback` when they're already present, instead of requiring
  manual host setup before the first `--network allow` run.
- Default network stays `none`; `inherit` remains the explicit escape hatch.

## [0.3.0] - 2026-07-24 — untagged, roadmap milestone only

### Added — Provider registry + verification pipeline (roadmap 0.3)
- **Registry extracted to data** (`providers/`): `PROVIDER_MAP` /
  `PKG_ARTIFACTS` and friends moved out of `probe.py`/`bakery.py` into
  schema-validated JSON, loaded by both modules; CI validates the schema.
- **Evidence-record format + L0–L4 ladder** implemented
  (`registry.schema.json`'s `$defs/verification`).
- **Registry verification backfill** (`providers/backfill.py`): walks every
  `provider-map.json` entry, resolves each Linux binary's FreeBSD artifact
  path via bakery's own resolution tables, checks existence on a real
  FreeBSD host, and persists the result into `providers/verification.json`.
  The host-existence check is injected, so the module stays fully
  unit-testable on Linux with a fake checker.
- **`verify_runs()`**: climbs L1→L2 — for every entry at L1 ("exists"),
  executes its smoke command (default `--version`; per-binary overrides for
  tools that differ: `java -version`, `dash -c 'exit 0'`, `lua -v`,
  `openssl version`, `unzip -v`) inside a throwaway jail via an injected
  runner, and bumps to L2 ("runs") on exit 0.
- **Registry correctness fixes the backfill surfaced** — several shipped
  mappings were false promises that could never confirm: `clang`/`clang++`
  (`pkg:llvm` was a stub → `pkg:llvm22`), `gcc`/`g++` (`pkg:gcc` was a stub
  → `pkg:gcc16`), `scons` (→ `pkg:scons-py312`), `esphome` (no FreeBSD
  package exists at all — removed, falls back to linuxulator; the native
  xtensa toolchain mapping that actually matters for the ESP32 build stays),
  `riscv32-esp-elf-gcc` (no real port — removed), and several binaries whose
  package installs a differently-named executable (`php83`→`php`,
  `py311-pip`→`pip`, `python310`→`python3.10`, `openjdk21`→`java`).
- **Versioned multi-binary packages** (`providers/multi-binary-suffix.json`):
  `gcc16`/`llvm22` install only versioned binaries with no unversioned
  symlink; a per-package version-suffix map fixes resolution in both
  `bakery.fill_artifact_paths()` and the backfill resolver.
- **Registry expanded to 343 provider mappings** and a **weekly CI drift
  schedule** added. This grew the *mapping* count well past the ≥150 target,
  but verification evidence (`verification.json`) was not backfilled for the
  new entries — it still covers only the original 53, all at L2. See
  `ROADMAP.md`'s 0.3 status and 1.0 gate for the honest count.

### Fixed (2026-07-23)
- **`_merge_tree` special-file handling — contract fix (non-security).** A
  non-regular, non-symlink, non-directory OCI-layer entry (a FIFO, an
  AF_UNIX socket, or — as root — a char/block device node) reached
  `shutil.copy2()` in the final else branch of `store/store.py`'s
  `_merge_tree()`. `copy2()`/`copyfile()` raises `shutil.SpecialFileError`
  for a FIFO and a plain `OSError` for a socket — both `OSError` subclasses,
  neither a `store.StoreError`, so a caller catching only `StoreError` (the
  rest of `store.py`'s stated contract) wouldn't have caught this. The
  safety property always held — nothing was written into the rootfs, no
  partial state, no hang — so this was purely an exception-type contract
  fix, not a vulnerability. `_merge_tree` now detects a non-regular file
  explicitly and raises `StoreError` naming the offending entry and its
  file-mode type before `copy2` is ever called. Covered by
  `store/test_special_file_handling.py`.

## [0.2.0] - 2026-07-24

### Added — Legibility & operator UX (roadmap 0.2)
- **`ROADMAP.md` completed** — restored from a mid-sentence truncation that
  had left the 0.7-through-1.0 and beyond-1.0 sections missing.

## [0.1.0] - 2026-07-20

### rctl limits individually stress-tested (2026-07-20)
Closing a gap the 2026-07-19 run left open: `memoryuse`/`pcpu`/`maxproc`/
`writebps` were only reasoned "by analogy" to the confirmed `cputime` case.
Ran each in isolation against deliberately-bad input on a FreeBSD dev host
(`kern.racct.enable=1` live, confirmed via `sysctl`):
- `memoryuse:sigkill=100m` — CONFIRMED. A doubling-string memory bomb died
  (exit 247) around iteration 22-23 (~130-260MB), never reaching a
  40-iteration "survived" marker.
- `maxproc:deny=10` — CONFIRMED. A loop forking 50 background jobs stopped
  forking at exactly 9 (correctly refusing fork #10, counting the shell).
- `writebps:throttle=5m` — CONFIRMED. 50MB write took 10.53s (4.7MB/s,
  matching the limit) vs 0.01s / 4.6GB/s unthrottled — ~1000x difference.
- `pcpu:sigkill=10` — **NOT CONFIRMED, likely non-functional.** A
  single-threaded busy-loop ran 60s+ unaffected; `rctl -u` on the live jail
  showed `pcpu=100` (kernel tracks usage correctly, 10x over the sigkill
  threshold) with no signal ever delivered. `cputime:sigkill` remains the real
  CPU-runaway backstop — `pcpu:sigkill` kept in `DEFAULT_RCTL_RULES` (harmless)
  but should not be relied on or advertised as working. Root cause (FreeBSD
  quirk vs a jailrun rule-string issue) not yet diagnosed.
- `readbps:throttle` — attempted, inconclusive: the test file was still hot in
  ZFS ARC from being written moments earlier, so the fast read result isn't
  evidence either way. Same code path as the confirmed `writebps`; low risk,
  but worth a clean (cold-cache) re-test before fully trusting it.

Also: a batch of `--rm` jails from these tests appeared to leak (jail stayed
in `jls` after the run) — turned out to be a **test-harness artifact**, not a
real bug: wrapping the whole `jailrun run` invocation in an external `timeout`
SIGTERMs the Python process before its own `finally`-block teardown can run.
Re-running the same scenario with jailrun's own `--timeout` flag (which stops
just the jexec'd command, not the CLI process) left zero stray jails. Real
usage (a caller with its own timeout/process-group handling) is
extremely unlikely to reproduce this externally-SIGTERM'd shape, but worth
being aware of if `--rm` cleanup ever looks flaky again.

### First real end-to-end run (2026-07-19)
- **`jailrun run esphome/esphome:stable esphome compile blink.yaml` succeeded end
  to end through jailrun's own code** (`runtime.cli` → `runtime.engine` →
  `store.py`/`probe.py`/`bakery.py`) on a FreeBSD 15.1 host — the first time ever,
  producing real `firmware.factory.bin`/`firmware.ota.bin`/`firmware.elf` via a
  genuine ESP-IDF network fetch + cmake/ninja/gcc build. Getting here required
  fixing every bug listed below — none of engine.py's live code paths had ever
  actually executed before this run.
- All three sandbox hardening controls (network default-deny, subprocess
  timeouts, rctl limits) were exercised against deliberately-bad input and
  confirmed working: `ping` inside a default jail fails with "Protocol not
  supported"; a `--timeout 5` run against a deliberately-hung `sleep 300` reliably
  kills the whole jail (`jls` empty, zero orphaned processes) within one retry
  cycle of the fix below; a `cputime:sigkill=2` rule killed a CPU-bound busy-loop
  in ~2s (exit 247 = SIGKILL) with `kern.racct.enable=1` set (needs a host
  reboot — a loader tunable, not toggleable live).

### Fixed (live E2E debugging, 2026-07-19 — bugs found only by actually running
the pipeline for real; every one of these had sat unexercised through every prior
"prove-out")
- `store.py`: `zfs create`/`zfs clone` lacked `-p` — the very first `unpack()` on
  a fresh pool failed with "parent does not exist" (`<pool>/images` etc. were
  never created by anything).
- `Store.__init__`'s `umoci` default flipped `True`→`False`: umoci is confirmed
  NOT packaged for FreeBSD at all; the bsdtar fallback (already hardened, see
  Security below) is what actually runs.
- `engine.py` imported `store.store` as a bare module and called
  `_store_module.resolve(...)` etc. directly on it — `Store`'s public API is on
  the CLASS, not module-level functions (unlike probe.py's `probe()`/bakery.py's
  `bake()`, which genuinely are module-level). `AttributeError` on the first real
  `resolve()` call. Fixed by instantiating `Store()`.
- `Path(rootfs_path + mp)` in the Linuxulator-mountpoint setup — `rootfs_path` is
  a `Path` (store.clone()'s real return type); `Path + str` raised `TypeError`.
- `jail_name = f"jailrun-{handle}"` stringified the WHOLE `Handle` dataclass repr
  instead of `handle.id` — `jail(8)` rejected the resulting name outright
  ("unknown parameter"). Also: `handle.jail_name` was never actually set, making
  `store.destroy()`'s own built-in `jail -r` step dead code in this call path.
- `_build_jail_conf`'s `path = {rootfs_path!r};` line rendered
  `PosixPath('/...')` (invalid jail.conf syntax) instead of a quoted string —
  `str()` first, then `!r`.
- The `linprocfs`/`linsysfs`/`tmpfs`/`fdescfs` `mount +=` lines used BARE paths
  (`/proc`, `/sys`, ...) — a jail.conf mountpoint field is an absolute HOST path,
  not resolved against the jail's own `path=`; `mount -t linsysfs linsysfs /sys`
  failed against the HOST's own `/sys`. Fixed to prefix with `rootfs_path`,
  matching how the (working) nullfs bind lines already did it.
- `-v` volumes were mounted TWICE: once directly via `store.mount()` (correct —
  FreeBSD jails share the host mount namespace, so this alone is sufficient) and
  AGAIN via a redundant jail.conf `mount +=` entry, which then failed with
  "Resource deadlock avoided" trying to nullfs-mount the same target twice.
  Removed the redundant jail.conf rendering entirely.
- `_stream_jexec`'s subprocess inherited jailrun's OWN stdin — fine when run
  interactively, but the bsdOS guest-agent's EXEC transport hands it something
  the jailed Python interpreter couldn't use ("Fatal Python error:
  init_sys_streams... Bad file descriptor"). `-it`/interactive is an explicit
  stub anyway (not implemented) — now `stdin=DEVNULL` unconditionally.
- `_pipe_lines` used `stream.readline()` with asyncio's default 64KB limit — a
  real build's progress output (long `\r`-updated lines, no `\n` for a stretch)
  exceeded it and crashed the whole run (`ValueError: Separator is not found,
  and chunk exceed the limit`). Switched to chunked `stream.read(65536)` — this
  is pure passthrough streaming, it never needed line boundaries at all.
- `Store.destroy()`'s zfs-destroy retry (from the earlier "dataset is busy" fix)
  wasn't aggressive enough for a REAL build's footprint (ccache/ninja workers,
  mmap'd toolchain binaries) — increased to 10 attempts and moved `-f` (safe:
  this is ephemeral per-run scratch space) to attempt 3 instead of the last 3.
- **Timeout didn't actually kill anything.** `proc.kill()` targets the tracked
  jexec/sh wrapper PID only — SIGKILL to a parent does not cascade to its own
  children (basic Unix signal semantics), so `jexec <jail> sh -c 'a; b; c'`'s
  child process for `b` survived, orphaned but still running and still jailed,
  long after the wrapper was "killed". Fixed to remove the WHOLE JAIL on timeout
  (`jail -r` kills by kernel-level jail membership, independent of process
  ancestry) instead of trusting the process tree. A second layer: the bounded
  `await asyncio.wait_for(proc.wait(), timeout=10.0)` around reaping the
  original wrapper — without this, a hung `proc.wait()` (observed live; kernel
  stack parked in `kqread`) blocked the code from ever reaching the `jail -r`
  call at all.
- **`rctl`'s `deny` action is a silent no-op for accumulating resources.**
  `deny` only means something for resources rctl(8) can check at the moment of
  the action (`maxproc`: refuse a new fork). For `cputime`/`memoryuse`/`pcpu`
  (usage that just keeps rising) there's nothing to "deny" — confirmed live: a
  `cputime:deny=2` rule let a CPU-bound busy-loop run unaffected, while
  `cputime:sigkill=2` correctly killed it in ~2s. `DEFAULT_RCTL_RULES` now uses
  `sigkill` for memoryuse/pcpu/cputime and `throttle` for disk I/O rate limits;
  `maxproc` keeps `deny` (the one case where it's actually correct).
- `bakery.py` called an internal `_MockStore` UNCONDITIONALLY since the module
  was written — the "replace this block with the real Store" comment sat there
  through every prior prove-out. `register_base()` returned a fake
  `zroot/jailrun/bases/<hash>` snapshot_id that engine.py's `base_mountpoint()`
  then "resolved" to a directory nothing had ever created. Wired to the real
  `store.store.Store`.
- Once wired to the real store, `pkg install`/`make install` with only
  `cwd=<mountpoint>` (no `-r`/`DESTDIR`) installed onto the LIVE HOST's real
  system — `cwd` does not redirect an install root. Added `JAILRUN_BASE_ROOT`
  (the base's own mountpoint) threaded through as an env var; `pkg -r
  "$JAILRUN_BASE_ROOT"` / `make ... DESTDIR="$JAILRUN_BASE_ROOT"`.
- A freshly `zfs create`d base dataset has no pkg trust-key directories of its
  own — `pkg -r <root>` looks for them UNDER that root, not the host's copy, so
  every repo catalog fetch silently failed signature verification ("Error
  opening the trusted directory"), and `pkg -r <root> update` reported "up to
  date" while never actually saving a usable catalog — the next `install` then
  failed with "Repository ... cannot be opened". Fixed by seeding
  `/usr/share/keys/{pkg,pkgbase-*}` into the fresh root before provisioning
  (`_seed_pkg_trust_keys`); also needed `pkg -r <root> update` before the very
  first `install` in that root (a fresh root has no cached catalog at all).
- `probe.py`'s `PROVIDER_MAP` had `"xz": "pkg:xz"` — there is no FreeBSD package
  named `xz` (it's shipped in the base system at `/usr/bin/xz`); removed so it
  correctly falls back to `linuxulator` instead of failing the whole
  provisioning plan.
- `bakery.py`'s `binutils` artifact resolution assumed one package = one binary
  matching the package name — `binutils` installs `ar`/`ld`/`nm`/`objcopy`/...,
  none of them literally named `binutils`. Every binutils-provided binary was
  pointed at a nonexistent `/usr/local/bin/binutils` (correctly skipped by the
  artifact-reality check, but never actually shadowed). `fill_artifact_paths()`
  now uses the ORIGINAL binary's own basename for packages in the new
  `MULTI_BINARY_PKGS` set.
- `bin/jailrun`'s shim computed `$here` (the repo root) but never used it —
  `python3 -m runtime.cli` only resolves if cwd already happens to be the repo
  root, so the shim did not actually work "from any cwd" as documented. Now
  exports `PYTHONPATH`.
- `probe/` and `bakery/` had no `__init__.py` (only `store/` did) — relied on
  Python's implicit namespace-package fallback, which pytest's own test
  collection didn't handle for a new `bakery.bakery`-style import. Added
  `bakery/__init__.py` (matching store/'s existing one; pyproject.toml already
  declared both as packages).
- `schemas/substitution-manifest.schema.json`'s `additionalProperties: false`
  rejected `bake()`'s own `_bakery` output against its own contract — same class
  of "never actually run together" bug as everything above. Declared `_bakery`
  explicitly.

### Security (2026-07-19)
- **Second OCI-layer symlink-escape, not covered by the original `_merge_tree` fix.**
  `_unpack_bsdtar`'s
  opaque/file whiteout processing ran BEFORE the fixed `_merge_tree`, over rootfs
  state an earlier layer could legitimately have planted a symlink into (e.g.
  `usr/evil -> /etc`) — a later layer's whiteout marker under that path could
  delete-through-symlink onto the host. Fixed with the same fail-closed `_within()`
  guard as `_merge_tree` (`_clear_opaque_whiteout` / `_apply_file_whiteout`); 3 new
  regression tests.
- **`-v host:ctr` destination containment.** `store.mount()` joined the container
  destination onto the rootfs without checking for `../` escape — mostly an
  operator-trust-boundary concern, but a caller may build the
  destination from untrusted user data. Now refuses (fail-closed) before any
  `mkdir`/`mount_nullfs`.
- **Network egress default-deny.** Jails previously got `allow.raw_sockets`
  unconditionally and implicit `ip4=inherit` (full host network). Default is now
  `ip4=disable`/`ip6=disable`, raw sockets off — opt-in via `--network inherit` /
  `--allow-raw-sockets` for commands that genuinely need it.
- **rctl resource limits** (CPU time, memory, process count, disk I/O) now applied
  to every jail via `rctl(8)`, degrading gracefully (warns, skips) if `racct` isn't
  enabled on the host. Defaults are provisional — sized generously, not yet
  profiled against a real build.
- **Subprocess timeouts everywhere.** `store.py`'s `_run`/`_run_ok` and `engine.py`'s
  `_run_subprocess`/`_stream_jexec` previously had none — a hung fetch or wedged
  build had nothing that would kill it. Tiered defaults (60s local ops / 600s
  network fetch / 300s layer extraction / 3600s port provisioning / 1800s jexec);
  all overridable via env vars or `--timeout`.

### Fixed (2026-07-19 — contract-drift bugs found by static audit + reading)
- `deploy/prove-on-freebsd.sh` imported nonexistent `probe.probe.Probe` and
  `bakery.bakery.Bakery` classes (both modules only ever exported module-level
  functions) — the failures were silently swallowed by `|| WARN ... non-fatal`, so
  S2/S4 had never actually executed inside this runbook despite its own stated
  purpose. Fixed to call the real `probe()`/`bake()` functions.
- The runbook's S1 step hand-rolled `jail -c`/`jexec`/`jail -r` directly, meaning
  `runtime/engine.py` — the actual product — had **never been executed on FreeBSD**
  despite prior "thesis proven" claims (those validated the raw mechanism by hand,
  not jailrun's own code). Now calls `runtime.cli.main(["run", ...])` for real.
- `bakery.bake()` called `store.register_base(name, plan_dict)`, but
  `store.store.Store.register_base()`'s real signature expects a shell-command
  **string** (it runs `sh -c provision_cmd`) — passing a dict would have TypeError'd
  on the very first real `bake()` call. Added `plan_to_provision_cmd()` (renders
  pkg/port steps; refuses — rather than mis-executes — plans containing `build:`
  from-source recipes, which need human review per `bakery/recipes/*.md`).
- **Native-shadow symlinks pointed at a path invisible from inside the jail.**
  `_assemble_native_shadow` symlinked directly to `native.artifact_path` (e.g.
  `/usr/local/bin/python3.11`) on the assumption the run's clone "inherited" the
  bakery-registered base dataset — it never did; the base is a wholly separate
  ZFS/plaindir snapshot that nothing mounted into the clone. Added
  `Store.base_mountpoint()` + engine.py now bind-mounts the base at
  `/jailrun-native/base` before building shadow symlinks, and enforces
  ARCHITECTURE.md's artifact-reality invariant (verifies the target exists,
  through the mount, before ever shadowing it — never a phantom binary).
- `schemas/substitution-manifest.schema.json`'s `additionalProperties: false`
  rejected `bake()`'s own `_bakery` output against its own contract. Declared
  `_bakery` explicitly in the schema.

### Added
- Architecture + the two contract seams: the Substitution Manifest
  (`schemas/substitution-manifest.schema.json`) and the Store API (`ARCHITECTURE.md`).
- **store/** — ZFS-native OCI store (skopeo + umoci, `zfs clone`, `mount_nullfs`).
- **probe/** — ELF-`EI_OSABI` binary classifier → substitution manifest; Linuxulator
  smoke harness; Linux→FreeBSD provider map. Classifier has passing unit tests.
- **bakery/** — provider → native-artifact resolver (`pkg:` / `port:` / `build:`) and
  recipes (xtensa-esp32 port ready; lx106 / riscv32 to build).
- **runtime/** — `docker run`-compatible CLI + hybrid engine (native-first PATH
  shadowing; Linuxulator only when the manifest requires it).
- Cross-seam invariants (artifact-reality; derived `linuxulator.required`;
  live-smoke-over-static-gaps).
- **CI** (`.github/workflows/ci.yml`, py 3.10/3.11/3.12): py_compile, shell syntax
  check on every `*.sh`, `pytest` (19 tests: ELF classifier, symlink-escape ×3
  variants, mount containment, `base_mountpoint()`), and a schema-validation smoke
  test that runs the real `probe()`/`bake()` against a synthetic rootfs. Exists so
  the contract-drift bugs above get caught automatically instead of sitting
  unnoticed in a runbook.

### Validated (FreeBSD 15.1 dev VM, mechanism level)
- **Native ESP32 build — proven.** `devel/xtensa-esp-elf` (a FreeBSD-ELF toolchain) + esp-idf
  compile an ESP32 `blink` firmware with **zero Linux ABI** (Linuxulator not loaded). The
  native-first substitution's load-bearing case works.
- **OCI + Linuxulator — proven.** `skopeo` pull + `bsdtar` unpack of a Linux `busybox` image →
  executed under Linuxulator in a jail (`uname` → `Linux x86_64`). The irreducible-Linux
  fallback works.

### Changed
- Store unpack: `umoci` is **not** packaged for FreeBSD → `bsdtar` is the primary OCI-layer
  unpack path (`umoci` optional). `skopeo copy --override-os linux` confirmed on FreeBSD 15.
- All source annotated with **sema** semantic-markup contracts.

[Unreleased]: https://github.com/bzdOS/jailrun/compare/v1.0.0-alpha.2...HEAD
[1.0.0-alpha.2]: https://github.com/bzdOS/jailrun/compare/v1.0.0-alpha.1...v1.0.0-alpha.2
[1.0.0-alpha.1]: https://github.com/bzdOS/jailrun/compare/v0.9.0...v1.0.0-alpha.1
[0.8.0]: https://github.com/bzdOS/jailrun/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/bzdOS/jailrun/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/bzdOS/jailrun/compare/v0.2.0...v0.6.0
[0.2.0]: https://github.com/bzdOS/jailrun/compare/b189813...v0.2.0
[0.1.0]: https://github.com/bzdOS/jailrun/commit/b189813

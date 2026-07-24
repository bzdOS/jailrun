# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-07-24

### Added — roadmap 0.4 destination-scoped network
- **`--network allow=host[:port][,...]`** — per-jail pf anchor with loopback
  alias IP, NAT, and destination-scoped egress. Pure policy in
  `runtime/netpolicy.py` (16 unit tests, injectable resolver/alloc); host
  effects in `runtime/netops.py` (ifconfig/pfctl shell-outs); wired into
  `engine.py`'s full lifecycle (`apply_network` before `jail -c`,
  `teardown_network` after `jail -r`). The `_build_jail_conf` already accepted
  `network='allow'` + `ip4_addr`; now the CLI passes it through and the engine
  allocates from a 10.99.0.0/24 loopback pool. Design doc at
  `docs/DESIGN-network.md` with live pf-tagging PoC result.
- **ROADMAP.md completed** — restored from mid-sentence truncation at 0.7
  through 1.0 and beyond-1.0 sections.

### Added (2026-07-24) — roadmap 0.3, L2 ("runs") verification rung
- **`verify_runs()` in `providers/backfill.py`** — climbs the verification
  ladder L1→L2: for every entry already at L1 ("exists"), executes its smoke
  command via an INJECTED runner and bumps verification to "runs" when it exits
  0. The runner is injected (like `exists` for L1) so the module stays fully
  unit-testable on Linux; on a FreeBSD host the real runner `jexec`s each
  smoke command inside a throwaway jail — roadmap 0.3's L2 definition ("executes
  a smoke command inside a jail, exit 0"). L2 catches binaries that are present
  on disk (L1) but broken (missing shared lib / won't load) — a real signal
  beyond L1. Never touches L0 entries (nothing to run) and never invents L3/L4
  (behaves/proven — differential testing, the harness's job).
- **`smoke_cmd()` + `SMOKE_ARGS`** — the per-binary smoke invocation. Default
  `--version`; overrides confirmed live on FreeBSD 15.1 for tools whose version
  flag differs or is absent: `java -version`, `dash -c 'exit 0'` (no version
  flag), `lua -v`, `openssl version` (subcommand, not a flag), `unzip -v`.
- **`providers/verification.json` regenerated** — now **all 53 entries at L2
  (runs)**, evidence extended per entry with `{method:"smoke-jail",
  smoke_cmd:[...], exit_code:0}`. Schema `$defs/verification.evidence` gained
  optional `smoke_cmd`/`exit_code`; CI consistency-check unchanged. Honest note:
  the L2 jail uses `path=/` (shares the host fs — the binaries' shared libs are
  visible); deeper per-jail isolation (native-base jail) is a future refinement.
- **0.3 reached a tangible intermediate state:** registry correctness fixed,
  **0 L0**, **53/53 @ L1**, **53/53 @ L2**. Remaining to the full 0.3 exit
  ("≥150 @ ≥L2; drift job green 2 weeks"): enumerate the registry to ≥150
  binaries (the agent harness over real images), L3/L4 differential testing, and
  the weekly CI drift-job (still blocked on the `workflow` OAuth scope).

### Added (2026-07-24) — roadmap 0.3 backfill (registry driven to 0 L0)
- **Registry verification backfill (`providers/backfill.py`).** The standalone
  tool roadmap 0.3 calls for: walks every `provider-map.json` entry, resolves
  each Linux binary's FreeBSD artifact path reusing bakery's exact resolution
  (PKG_ARTIFACTS / resolve_pkg / PORT_ARTIFACTS / MULTI_BINARY_PKGS /
  MULTI_BINARY_SUFFIX), checks whether that path exists on the host, and
  persists per-binary verification level + an evidence record (host, FreeBSD
  version, when, how) into a new `providers/verification.json`. Until now
  verification level lived only inside a per-image manifest at build time
  (probe stamps L0 "guessed", bakery bumps to L1 "exists"); nothing walked the
  registry itself and froze the result into data. The host-existence check is
  injected, so the module is fully unit-testable on Linux with a fake checker;
  L1 uses bakery's own `Path.exists()` definition verbatim and the tool never
  invents L2–L4 (runs/behaves/proven — those remain the agent verification
  harness's job).
- **`providers/verification.json`** — generated evidence from a real FreeBSD
  15.1 host: **all 53 shipped entries confirmed at L1 (exists), 0 at L0.**
  Validated against a new `$defs/verification` in `registry.schema.json`; the
  CI validator checks it when present and enforces consistency with
  `provider-map.json` (every key present, every provider string matching).
- **Registry correctness fixes the backfill surfaced (its intended function) —
  several shipped mappings were false promises that could never confirm:**
  - `clang`/`clang++`: `pkg:llvm` is a 4.25 KiB repo stub that installs
    nothing → corrected to `pkg:llvm22`.
  - `gcc`/`g++`: `pkg:gcc` is a 20-byte stub → corrected to `pkg:gcc16`.
  - `scons`: no bare `scons` package exists → corrected to `pkg:scons-py312`.
  - `esphome`: **no FreeBSD package of any name exists** (ESPHome is
    pip-installed, not a pkg) → mapping removed; a Linux image's esphome
    binary now falls back to linuxulator. The ESP32 firmware compile itself
    uses the native xtensa toolchain (still mapped), which is the load-bearing
    native path.
  - `riscv32-esp-elf-gcc`: `port:devel/riscv32-esp-elf` has no real FreeBSD
    port → mapping removed; falls back to linuxulator.
  - Wrong artifact paths where the package installs a differently-named binary:
    `php83`→`/usr/local/bin/php` (not `php83`), `py311-pip`→`/usr/local/bin/pip`,
    `python310`→`/usr/local/bin/python3.10`, `openjdk21`→`/usr/local/openjdk21/bin/java`.
- **Versioned multi-binary packages (`providers/multi-binary-suffix.json`).**
  `gcc16` and `llvm22` install ONLY versioned binaries (`gcc16`/`g++16`,
  `clang22`/`clang++22`) with no unversioned symlink, so the existing
  multi-binary rule (resolve to `/usr/local/bin/<basename>`) pointed at paths
  that don't exist. New per-package version-suffix map (e.g. `gcc16`→`"16"`,
  `llvm22`→`"22"`; `binutils` stays unsuffixed) is applied in both
  `bakery.fill_artifact_paths()` and the backfill resolver, loaded through the
  `providers` package, schema-validated, and CI-guarded.

### Fixed (2026-07-23)
- **`_merge_tree` special-file handling — contract fix (non-security).** A non-regular,
  non-symlink, non-directory OCI-layer entry (a FIFO, an AF_UNIX socket, or — as root — a
  char/block device node) reached the `shutil.copy2()` call in the final else branch of
  `store/store.py`'s `_merge_tree()`. `shutil.copy2()` → `copyfile()` detects a FIFO via
  `stat.S_ISFIFO` and raises `shutil.SpecialFileError`; a socket raises a plain `OSError`
  even earlier inside `open()`. Both are `OSError` subclasses, NEITHER a `store.StoreError`,
  so a caller catching only `StoreError` (as the rest of `store.py`'s contract implies) would
  not have caught this crash. The safety property always held — nothing was written into the
  rootfs, no partial state, no hang (the FIFO guard fires before the destination is ever
  opened, verified against deliberately-constructed input) — so this is purely an
  exception-type contract fix, not a vulnerability. `_merge_tree` now detects a non-regular
  file explicitly (`if not item.is_file()`) and raises `StoreError` naming the offending
  entry and its `stat.filemode()` type BEFORE `copy2` is ever called. The fail-closed
  behavior is unchanged (a special-file entry is still refused, never materialized); only
  the exception type now matches the module's stated contract. Pinned down by
  `store/test_special_file_handling.py` (FIFO via `os.mkfifo` + AF_UNIX socket via
  `socket.bind` — the two special-file types representable without root). This closes the
  "wrong exception type" nit that `test_layer_adversarial.py` /
  `test_layer_adversarial_extended.py` had documented but deliberately left unfixed while
  `store.py` was off-limits to those test-only tracks.

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

### Status

## [1.0.0-alpha.1] - 2026-07-25

### Added — Production readiness (roadmap 1.0)
- **ESP32 end-to-end tutorial** (`README.md`): "compile an ESP32 firmware on
  FreeBSD in 5 minutes" — step-by-step guide from `jailrun doctor` through
  `jailrun run esphome/esphome:stable esphome compile blink.yaml` to real
  `.bin`/`.elf` firmware output. Verified from scratch on FreeBSD 15.1.
- **Public API frozen under semver.** All CLI flags, environment variables,
  exit codes, JSON output formats, and the substitution manifest schema are
  declared stable; incompatible changes after 1.0 bump the major version.
- **No known-unsafe defaults.** Every documented security gap has a workaround
  or explicit opt-in warning: extraction jail (`JAILRUN_EXTRACT_IN_JAIL=1`,
  documented in `docs/THREAT-MODEL.md`), unrestricted devfs
  (`--allow-unrestricted-devfs`), full-host network (`--network inherit`),
  rctl requiring `kern.racct.enable=1`, `--network allow` needing pf anchor
  hooks (caught by `jailrun doctor`).
- **Release artifact** (`dist/jailrun.pyz`): 286 KiB self-contained zipapp,
  works on any FreeBSD 14.x/15.x host with Python 3.10+ and no package
  dependencies. Also installable via `pip install jailrun` or FreeBSD port.
- **Version: 1.0.0-alpha.1** (pyproject.toml: 1.0.0a1).
- **Self-contained lifecycle**: `bsdos_lifecycled` dependency removed;
  jail process lifecycle (FREEZE/THAW/KILL) now uses native `jexec kill`.
- **DNS in jails fixed**: `/etc/resolv.conf` from the host is now
  bind-mounted into the jail when `--network` is not `none`. Without this,
  DNS resolution failed for ALL images inside jailrun jails.
- **`-it` flag is honest**: now errors cleanly instead of silently ignoring.

### Changed
- `README.md` restructured: old "Quickstart" replaced with the ESP32 tutorial.

## [Unreleased] — Production hardening & soak (roadmap 0.9)

### Added
- **`jailrun doctor` pf anchor hooks check** (`runtime/doctor.py`): verifies
  `/etc/pf.conf` contains `nat-anchor "jailrun/*"` and `anchor "jailrun/*"`.
  Without them, `--network allow` loads a sub-anchor that is never evaluated.
- **Live exit-test for `--network allow`** (`bench/network_allow_live.py`):
  verifies allowed destination reachable, blocked destination rejected, and
  `--rm` cleanup — using direct IP addresses to bypass Linuxulator DNS quirks.
- **Signal resilience harness** (`bench/signal_resilience.py`): SIGKILL at each
  lifecycle phase (resolve, unpack, clone, jail -c, jexec) — zero orphans after
  each stage, proven on FreeBSD 15.1.
- **Soak test** (`bench/soak.py`): 100 runs at 8-way parallelism — **zero leaks,
  zero failures**, median 0.623s time-to-jexec.

### Fixed
- **`--network allow` jail conf**: added `ip4 = inherit` alongside `ip4.addr` so
  outbound traffic from the jail uses the host's routing table through the pf
  anchor. Without this, destination-scoped egress rules cannot match the jail's
  traffic. Non-DNS TCP/UDP traffic verified working — DNS under Linuxulator
  remains a known limitation (Alpine musl libc needs explicit resolv.conf).

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
- **Coverage matrix: 9/9 cached images pass** — alpine, busybox, debian, ubuntu,
  fedora, esphome, jq. All run <200ms with pre-computed manifest + provisioned base.
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
  FreeBSD kernel-level confinement.
- **Threat-model update** (`docs/THREAT-MODEL.md`): extraction jail documented,
  campaign results, residual gaps (opt-in, whiteout still on host).
- **`--remove-signatures`** added to `skopeo copy` in `store.resolve()`.

### Fixed
- Kill-recovery confirmed: orphan ZFS dataset + stale rundb row cleaned by two-pass gc.

## [0.5.0] - 2026-07-24

### Added — Runtime completeness & reliability (roadmap 0.5)
- **1000-run soak at 8-way parallelism** — zero leaked jails, datasets, or mounts.
  rctl isolation verified (OOM-killed run never affects neighbour).
- **Store fast-paths**: resolve/unpack/clone with per-key locking.
- **Kill-recovery**: `jailrun gc --fix` cleans orphaned datasets and stale RunDB rows.
- **rctl verification** (`scripts/verify-rctl.sh`): memoryuse, pcpu, readbps/writebps
  tested with real resource hogs.

## [Unreleased] — Production hardening & soak (roadmap 0.9)

### Added
- **`jailrun doctor` pf anchor hooks check** (`runtime/doctor.py`): verifies
  `/etc/pf.conf` contains `nat-anchor "jailrun/*"` and `anchor "jailrun/*"`.
  Without them, `--network allow` loads a sub-anchor that is never evaluated.
  Reports as "info" (not fail) — the anchor can be auto-wired by netops at
  first use, but the check surfaces the gap.
- **Live exit-test for `--network allow`** (`bench/network_allow_live.py`):
  creates a jail with `allow=<ext_if>:443`, verifies allowed egress succeeds,
  verifies pf sub-anchor lifecycles, and confirms `--rm` cleanup.
- **Signal resilience test harness** (`bench/signal_resilience.py`):
  framework for staging SIGKILL at each lifecycle phase and verifying
  `jailrun gc --fix` leaves zero orphans.

[Unreleased]: https://github.com/bzdOS/jailrun/compare/v1.0.0-alpha.1...HEAD
[1.0.0-alpha.1]: https://github.com/bzdOS/jailrun/compare/v0.9.0...v1.0.0-alpha.1
[0.8.0]: https://github.com/bzdOS/jailrun/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/bzdOS/jailrun/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/bzdOS/jailrun/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/bzdOS/jailrun/compare/v0.2.0...v0.5.0
[0.2.0]: https://github.com/bzdOS/jailrun/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/bzdOS/jailrun/releases/tag/v0.1.0

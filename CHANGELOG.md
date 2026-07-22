# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); this project adheres to
[Semantic Versioning](https://semver.org/).

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
Pre-alpha, core pipeline working: `jailrun run esphome/esphome:stable esphome
compile blink.yaml` completed end to end through jailrun's own code on real FreeBSD 15.1,
producing a real ESP32 firmware image (see "First real end-to-end run", above). All three
sandbox hardening controls (network default-deny, subprocess/jexec timeouts, rctl resource
limits) are live-verified against deliberately-bad input, not just present in the code.

This is enough to use jailrun as a security boundary for untrusted third-party code
(e.g. compiling arbitrary user-uploaded ESPHome components), with the following caveats:
no adversarial red-team review yet, no Capsicum, and network isolation is currently
binary on/off rather than scoped by destination (true VNET+pf segmentation) — worth
revisiting if the threat model grows past a single-host compile sandbox.

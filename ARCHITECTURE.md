# jailrun — native-first OCI runtime for FreeBSD jails

> Working name (provisional). A `docker run`-shaped tool that runs OCI images in
> **FreeBSD jails**, substituting **native FreeBSD binaries** for the image's Linux
> ones wherever an equivalent exists, and falling back to **Linuxulator only for
> the irreducible**. ZFS is the image/layer store. Grew out of an ESPHome-compile
> use case; aligned with bsdOS (jails + Linuxulator + lifecycled). 2026-06-30.
>
> HONEST STATUS: greenfield systems software. It only *means* anything when run on
> FreeBSD (jails/ZFS/Linuxulator). Agents on this host (Linux/linux-host) produce
> DESIGN + SCAFFOLD + freebsd-host-runnable scripts — NOT a validated runtime. Mark every
> unproven step `# UNVERIFIED`. Validation is serial, on freebsd-host, the user's zone.

## Thesis
The container/isolation primitive is solved — jails do it, ZFS gives images/layers
(snapshot=image, clone=writable-layer) for free. Docker's moat is **the Linux
payload**, not the runtime. So the invention is not "emulate Docker" but **shrink
the Linux-ABI surface to the minimum, per binary**: run native where we can, use
Linuxulator only where we must. esphome is the first proof: the load-bearing Linux
bit is the ESP toolchain → substitute the native `xtensa-esp-elf` port → the rest
is a plain jail.

## The system = 4 subsystems of ONE runtime (not 4 separate projects)
```
runtime/  (S1)  hybrid exec engine — the `jailrun run` core
   ▲ consumes manifest (S2) + native artifacts (S4) + rootfs (S3)
probe/    (S2)  compat intelligence — emits the SUBSTITUTION MANIFEST
   │ directs ↓ (what's load-bearing, where Linuxulator breaks)
bakery/   (S4)  native supply — pkg/port/build the native replacements
store/    (S3)  ZFS-native OCI store — pull/unpack/clone rootfs (foundation)
```
Data flow: `store.unpack(image)` → `probe(image)`→manifest → `bakery.bake(manifest)`
fills native artifacts → `runtime.run(image)` assembles a jail (native base ∪ image
non-native parts), routes each exec native-first, Linuxulator only if the manifest
says so → stream → teardown.

## THE CONTRACT (two seams — everything hangs off these; do not diverge)

### Seam 1 — Substitution Manifest  (`schemas/substitution-manifest.schema.json`)
The central artifact. **probe (S2) PRODUCES it; bakery (S4) FILLS `native`;
runtime (S1) CONSUMES it.** Shape:
```json
{
  "image": "esphome/esphome:2025.5",
  "binaries": [
    { "path": "/usr/bin/python3", "role": "load-bearing", "abi": "linux",
      "status": "native",
      "native": { "provider": "pkg:python311", "artifact_path": "/usr/local/bin/python3.11" },
      "syscalls_needed": ["..."], "notes": "" },
    { "path": "/root/.platformio/.../xtensa-esp32-elf-gcc", "role": "load-bearing",
      "abi": "linux", "status": "native",
      "native": { "provider": "port:devel/xtensa-esp-elf", "artifact_path": "/usr/local/bin/xtensa-esp32-elf-gcc" } }
  ],
  "linuxulator": { "required": false, "gaps": [], "risk": "none" }
}
```
- `status`: `native` (substitute) | `linuxulator` (run Linux binary under ABI) |
  `missing` | `unknown`. runtime shadows native artifacts over the image's binaries
  in `PATH`; enables `linux64`/linprocfs ONLY if any binary is `linuxulator` or
  `linuxulator.required`.

### Seam 2 — Store API  (S3 implements; S1 + S4 call)
Language-agnostic (prototype in Python, shells to zfs/skopeo/jail):
```
resolve(image_ref)            -> image_id          # skopeo pull if absent
unpack(image_id)              -> snapshot_id        # layers -> ZFS dataset -> snapshot ("image")
register_base(name, provision)-> snapshot_id        # a pkg/port-provisioned NATIVE base (from bakery)
clone(snapshot_id)            -> (rootfs_path, handle)   # CoW writable layer for one run
mount(handle, binds=[(host,dest,ro)])               # nullfs binds (docker -v)
destroy(handle)                                     # jail -r + zfs destroy + unmount  (docker --rm)
```
Content-addressing: snapshot keyed by the ordered layer digests; native bases keyed
by their provision recipe hash.

## Subsystem responsibilities + I/O
- **S3 store/** — IN: image ref / base recipe. OUT: ZFS snapshots + the Store API
  above. Owns skopeo→bsdtar/umoci→zfs, nullfs, destroy. No knowledge of S1/S2/S4.
- **S2 probe/** — IN: an unpacked rootfs (from S3). OUT: a Substitution Manifest +
  a compat report. Classifies each binary (abi/role), proposes `native.provider`
  candidates (leaves `artifact_path` for S4), runs incremental smoke under
  Linuxulator, records syscall gaps. ALSO the public "does-it-run-on-FreeBSD" matrix.
- **S4 bakery/** — IN: a manifest with `native.provider` proposals. OUT: resolved
  native artifacts (pkg installed / port built / source built) registered as a base
  via S3.register_base; fills `native.artifact_path` + sets `status: native`.
- **S1 runtime/** — IN: image ref. Orchestrates S3+S2+S4, assembles the jail,
  native-first exec, stream+rc, teardown. Exposes the `docker run`-compatible CLI
  `jailrun run [--rm] [-v h:c[:ro]] [-e K=V] [-w dir] IMAGE CMD...`.

## Reference integration test (on freebsd-host, serial)
`jailrun run esphome/esphome:2025.5 compile /config/blink.yaml` — toolchain binaries
native-substituted (xtensa-esp-elf port), everything else native-or-Linuxulator, in a
ZFS-cloned jail, streamed, `--rm`. First green = the whole thesis demonstrated.

## Agent lanes (this wave — write ONLY your subdir, against this contract)
- S1 → `runtime/`  · S2 → `probe/`  · S3 → `store/`  · S4 → `bakery/`
- Shared, READ-ONLY for agents: `ARCHITECTURE.md`, `schemas/`.
- Prototype language: Python 3 orchestrator + shell-outs (so it can later back a
  `ContainerBackend` abstraction directly). No run, no git, no pkg install — design +
  scaffold + `*.freebsd.sh` scripts marked `# UNVERIFIED`.

## Cross-seam invariants (binding — added after the first integration pass)

The 4 subsystems were built independently against the seams above; these
invariants are what keep them honest together. All three (probe/bakery/runtime)
MUST uphold them.

1. **Artifact-reality.** `status: "native"` ⟺ a real native artifact WILL exist at
   `native.artifact_path` in the assembled rootfs. **bakery** sets `status:native`
   ONLY when its recipe actually resolves to an artifact (recipe maturity `ready`,
   or `experimental` that built); a `stub`/unresolved recipe (e.g.
   `build:riscv32-esp-elf` today) must downgrade the binary to
   `status:"linuxulator"` (if the image's Linux binary can run under the ABI) or
   `status:"missing"`. **runtime** MUST verify `artifact_path` exists at assembly
   time before shadowing; on a miss → downgrade that binary to `linuxulator` (and
   recompute the gate), or fail the run if the Linux binary also cannot run.
   (Closes S4 open-Q4: never shadow a phantom binary.)
2. **`linuxulator.required` is derived, never hand-set.** It = OR over all binaries
   with `status=="linuxulator"`. Any status downgrade (rule 1) must recompute it,
   so the runtime's "plain jail vs Linux-ABI" gate stays correct.
3. **The static syscall-gap list is advisory; the live smoke is authoritative.**
   probe's built-in "known gaps" (e.g. it lists `inotify`) is a hint only — several
   are already fixed (inotify landed in FreeBSD 15.0/14.4). `probe/smoke.freebsd.sh`
   (truss/ktrace on freebsd-host) is the source of truth for `linuxulator.gaps`; don't hard-
   block a run on the static list.

## Jail lifecycle backend = bsdos_lifecycled (bzdOS)

jailrun does NOT hand-roll jail process control. The running jail's process
lifecycle is delegated to **`bsdos_lifecycled`** — the bzdOS jail lifecycle daemon
(source `bsdOS/lifecycled`, prebuilt via bsdOS's own build/deploy pipeline,
`rc.d bsdos_lifecycled`). Reused, not reinvented.

**Responsibility split**
- jailrun (store + runtime): create / mount (nullfs) / `jexec` / ZFS clone+destroy.
- bsdos_lifecycled: the running jail's processes — `FREEZE` (SIGSTOP all PIDs),
  `THAW` (SIGCONT), `HIBERNATE` (ZFS snapshot + SIGSTOP), `RESTORE`, `KILL`
  (SIGKILL + cleanup). PID-targeted via `jail_get(2)` + `sysctl(KERN_PROC_PROC)`.

**Wire protocol** (`runtime/lifecycle.py`): AF_UNIX socket
`/var/run/bsdos-lifecycle.sock` (env `JAILRUN_LIFECYCLED_SOCK`); one line
`"<VERB> <jail_name>\n"`, JSON response; `jail_name == "jailrun-<handle>"`. A Zenoh
mirror (`bsdos/ctl/lifecycle`) exists for FREEZE/THAW/STATUS.

**Teardown path** (`engine.py`): on exit → lifecycled `KILL` (best-effort, if the
daemon is up) → always `jail -r` (remove the persist jail) → on `--rm`
`store.destroy` (zfs destroy + unmount). A missing daemon is fine — `jail -r`
handles it.

**Bonus jailrun gets for free:** `jailrun freeze/thaw/hibernate/restore <jail>` —
e.g. HIBERNATE a warm toolchain jail between compiles instead of tearing it down.
RAM-aware: directly mitigates the kind of OOM pressure seen on small FreeBSD hosts.
These are lifecycled-ONLY features (no native fallback) — honest `NotAvailable`
without the daemon.

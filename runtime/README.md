# jailrun runtime (S1)

**Native-first OCI exec engine for FreeBSD jails.**
This subsystem turns `jailrun run IMAGE CMD` into a running FreeBSD jail —
substituting native FreeBSD binaries for Linux ones wherever an equivalent
exists, and enabling Linuxulator only for the irreducible remainder.

---

## The `docker run` → jail lifecycle

```
jailrun run [FLAGS] IMAGE CMD
       │
       ▼
 cli.py — parse flags (--rm, -v, -e, -w, -it, IMAGE, CMD)
       │
       ▼
 engine.run(image_ref, cmd, opts)
       │
       ├─ 1. store.resolve(IMAGE)      → image_id   (skopeo pull if absent)
       ├─ 2. store.unpack(image_id)    → snapshot_id (OCI layers → ZFS snapshot)
       ├─ 3. store.clone(snapshot_id)  → (rootfs_path, handle)  (CoW writable layer)
       │
       ├─ 4. load substitution manifest
       │      └─ fast path: <rootfs>/.jailrun/substitution-manifest.json
       │      └─ slow path: probe(rootfs) → bakery.bake(manifest) → write cache
       │
       ├─ 5. assemble jail
       │      ├─ 5a. native-first PATH shadowing (see below)
       │      ├─ 5b. store.mount for -v bind mounts (nullfs)
       │      └─ 5c. Linuxulator-only-if-needed gate (see below)
       │
       ├─ 6. write jail.conf + jail -c JAIL_NAME
       │
       ├─ 7. jexec JAIL_NAME CMD   (asyncio, line-buffered stdout+stderr)
       │      └─ exact exit code returned (NOT exec.start's dispatch rc)
       │
       └─ 8. teardown
              ├─ jail -r JAIL_NAME
              └─ store.destroy(handle)   (only with --rm)
```

---

## Native-first PATH shadowing

The central mechanism: **native FreeBSD binaries shadow Linux binaries in PATH**
so the jail sees the native version first, without removing or patching the
image's Linux binaries.

### How it works

The bakery-registered base (the ZFS/plaindir dataset that actually holds the
provisioned native binaries) is a **separate** snapshot from the image's own
clone — an earlier version of this doc wrongly assumed the image clone
"inherited" the base.

1. If `manifest["_bakery"]["snapshot_id"]` is set, the engine resolves it to a
   host directory via `store.base_mountpoint()` and bind-mounts (nullfs, ro) it
   into THIS run's clone at `/jailrun-native/base` (`NATIVE_BASE_MOUNT`).
2. For every binary with `status: native` and a resolved `native.artifact_path`
   that verifiably exists **through that mount** (artifact-reality invariant —
   never shadow a phantom binary), the engine creates a symlink at:
   ```
   <rootfs>/jailrun-native/bin/<basename>  →  /jailrun-native/base<native.artifact_path>
   ```
   Example: `python3 → /jailrun-native/base/usr/local/bin/python3.11`
   (`python3.11` installed via `pkg:python311` into the base).
3. The engine prepends `/jailrun-native/bin` to `PATH` inside the jail:
   ```
   PATH=/jailrun-native/bin:/usr/local/bin:/usr/bin:/bin
   ```
4. When the jailed process execs `python3`, `execve` finds
   `/jailrun-native/bin/python3` first — the symlink resolving (through the
   base mount) to the native FreeBSD Python — and the Linux `/usr/bin/python3`
   is never touched.

### Why symlinks (over the mount), not a straight bind-mount per binary

- Symlinks live inside the CoW ZFS clone — cheap to create, cleaned up for free
  on `store.destroy`. Only the BASE itself needs an actual mount (one nullfs
  bind for the whole base, not one per substituted binary).
- No extra jail parameters needed per binary: the shadow dir is part of the
  rootfs the jail already sees.
- Auditable: `ls -la <rootfs>/jailrun-native/bin/` shows every substitution.

### Why `/jailrun-native/bin`

- Avoids colliding with `/usr/local/bin` or `/usr/bin` from either the base or
  the image.
- Namespaced: operators can inspect or remove the shadow layer without touching
  the image's file tree.

### Gotcha: nullfs uid passthrough

`-v HOST:CTR[:ro]` bind mounts are implemented with nullfs (FreeBSD's
equivalent of a bind mount). **nullfs has no uid/gid remap**: host uid numbers
appear unchanged inside the jail. If the jail user differs from the host file
owner, reads/writes may be denied. Workaround: align uids at the host layer, or
use a dedicated jail user that matches.

---

## Linuxulator-only-if-needed gate

The engine loads the Linux ABI (`kldload linux64`) and its pseudo-filesystems
**only** when the substitution manifest requires it.

### Decision logic

```python
linuxulator_required = (
    manifest["linuxulator"]["required"]          # probe/bakery set this
    or any(b["status"] == "linuxulator"          # at least one binary
           for b in manifest["binaries"])        # can't run natively
)
```

### When Linuxulator IS enabled

The jail gets four additional mounts (each via `mount +=` — additive, not
replacing, per FreeBSD `jail.conf` semantics):

| Filesystem | Mountpoint | Purpose |
|------------|------------|---------|
| `linprocfs` | `/proc` | Linux procfs view |
| `linsysfs` | `/sys` | Linux sysfs view |
| `tmpfs` | `/dev/shm` (mode 1777) | POSIX shared memory |
| `fdescfs -o linrdlnk` | `/dev/fd` | `/proc/self/fd` symlink fixup |

The `linrdlnk` option on fdescfs is critical: without it, Linux programs that
open files via `/proc/self/fd/<n>` get ENOENT because the symlinks resolve in
the FreeBSD namespace rather than the Linux one.

### When Linuxulator is NOT enabled

A plain FreeBSD jail — no `kldload`, no extra mounts, no Linux ABI overhead.
For images whose load-bearing binaries are fully substituted with native ones
(e.g. esphome after xtensa-esp-elf substitution), the result is an ordinary
FreeBSD jail that happens to contain the image's directory tree.

---

## The esphome example flow

```
jailrun run esphome/esphome:2025.5 compile /config/blink.yaml
```

1. store pulls + unpacks the esphome image.
2. probe classifies binaries; bakery resolves `xtensa-esp32-elf-gcc` →
   `port:devel/xtensa-esp-elf`, `python3` → `pkg:python311`, etc., and registers
   a native base via `store.register_base()`.
3. Manifest has `linuxulator.required: false` (all load-bearing binaries
   substituted natively).
4. Engine bind-mounts the registered base at `/jailrun-native/base`, then
   shadows native binaries in `/jailrun-native/bin/` (through that mount):
   - `xtensa-esp32-elf-gcc` → `/jailrun-native/base/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-gcc`
   - `python3` → `/jailrun-native/base/usr/local/bin/python3.11`
   - etc.
5. A **plain jail** is created — no `kldload linux64`, no linprocfs.
6. `jexec jailrun-<handle> compile /config/blink.yaml` streams the build log.
7. Exit code propagated; jail removed; ZFS clone destroyed (`--rm`).

First green run on freebsd-host = the entire thesis demonstrated: Linux payload running
natively in a FreeBSD jail with zero Linux ABI.

---

## Files

| File | Purpose |
|------|---------|
| `cli.py` | Argument parser; mirrors `docker run` flags |
| `engine.py` | Orchestration: resolve → unpack → clone → shadow → jail → jexec |
| `_mocks.py` | Stub seams for S2/S3/S4 (py_compile-clean on Linux) |
| `run.freebsd.sh` | Hand-driven smoke test (plain jail, alpine echo hi) — UNVERIFIED |

---

## Known limitations / what needs freebsd-host to verify

Updated 2026-07-19: `engine.py` is no longer unexercised — a real
`jailrun run esphome/esphome:stable esphome compile blink.yaml` ran end to end
through this code on FreeBSD 15.1, producing a real ESP32 firmware image. Every
bug items 1-2 below implied (and several more besides) was found and fixed live;
see CHANGELOG.md "Fixed (live E2E debugging, 2026-07-19)" for the full list.

1. ~~All of engine.py unexercised~~ — done; see above. Remaining open items:
2. **umoci unpack path** — moot for the proven path: umoci is confirmed NOT
   packaged for FreeBSD at all (`Store`'s default is now `umoci=False`, bsdtar).
   Only relevant if a host that genuinely has umoci installed opts back in.
3. **devfs ruleset** — `mount.devfs` in `jail.conf` may need an explicit
   `devfs_ruleset` on hardened FreeBSD configs; not hit in the esphome example
   run, but that run didn't specifically stress devfs-restricted configurations.
4. **rctl action semantics beyond cputime** — `sigkill` is empirically confirmed
   for `cputime`; `memoryuse`/`pcpu` use `sigkill` by analogy (same accumulating-
   resource category) but aren't individually stress-tested yet, and
   `readbps`/`writebps` use `throttle` (untested against real disk-saturating
   input).
5. **ZFS pool path** — `run.freebsd.sh` (the hand-driven smoke script, not the
   real pipeline) assumes `zroot/jailrun`; the real path (`JAILRUN_ZPOOL` env,
   default `jailrun`) is what's actually proven.

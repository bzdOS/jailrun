# jailrun — Development Environment

## Overview

Editing, `py_compile` syntax checks, and unit tests work on any machine
(Linux, macOS, etc.) — the Python source has no FreeBSD-only import-time
dependencies. **Actually running jailrun requires a real FreeBSD 15+ host**:
jails, ZFS, and the Linuxulator are FreeBSD kernel features and none of them
are emulable elsewhere.

---

## Recommended setup

- A FreeBSD 15+ VM (or bare-metal host) reachable from your dev machine —
  local libvirt/QEMU-KVM, a cloud FreeBSD instance, or bare metal all work
  equally; jailrun doesn't assume any particular hypervisor.
- A **dedicated ZFS pool** for jailrun, separate from the VM's root pool.
  OCI image unpacks, dataset clones, and clone chains can consume gigabytes
  quickly — filling the root pool takes the whole VM down with it, not just
  jailrun.
- Some way to get source onto the FreeBSD side: a plain `git clone`/`git pull`
  works fine; sharing the host checkout in read-only over 9p/virtfs (if your
  hypervisor supports it) avoids a copy-on-every-edit loop during active
  development.

### Dataset layout

Dataset layout (pool name = `jailrun`, no double-name):

```
jailrun/
  images/   ← unpacked OCI image datasets + @snap snapshots
  bases/    ← native-provisioned bases (bakery output) + @snap snapshots
  runs/     ← ephemeral CoW clones (one per jailrun run invocation)
```

The `JAILRUN_ZPOOL` environment variable controls the pool name (default:
`jailrun`). The layout is `<zpool>/images`, `<zpool>/bases`, `<zpool>/runs`
— not `<zpool>/jailrun/…` — to avoid the awkward `jailrun/jailrun` double name.

### 9p source delivery (optional)

If your hypervisor supports virtfs/9p, exposing the host checkout read-only
into the VM avoids re-copying on every edit:

```sh
# On the FreeBSD VM as root (FreeBSD 15 uses p9fs — verified working)
mount -t p9fs -o trans=virtio jailrun /mnt/jailrun
```

Add to `/etc/fstab` for persistence:

```
jailrun   /mnt/jailrun   virtfs   trans=virtio,version=9p2000.L   0   0
```

Set `PYTHONPATH=/mnt/jailrun` (or `JAILRUN_SRC=/mnt/jailrun`) so
`import store.store`, `import probe.probe`, etc. resolve from the shared
checkout without a copy step.

---

## Native-first: linux64 off by default

jailrun's thesis is **native substitution first**. The Linuxulator (linux64
kernel module) is NOT loaded by default.

- `kldload linux64` is not done by `provision-freebsd.sh` unless `--tier2`
  is passed.
- `linux64` is needed only for the **Tier-2 OCI fallback**: running Linux
  ELF binaries that have no native FreeBSD substitute.
- The esphome example (esphome + xtensa toolchain) uses native pkg/port
  substitutes for all load-bearing binaries, so the jail is a plain FreeBSD
  jail — no Linuxulator at all.
- `linuxulator.required` in the substitution manifest is derived from whether
  any binary has `status: "linuxulator"`. The runtime loads linux64 only
  when that field is true (see ARCHITECTURE.md invariant 2).

---

## Provision + prove sequence

```sh
# 1. Provision (idempotent — safe to re-run)
sh deploy/provision-freebsd.sh /dev/vtbd1

# 2. Full-stack prove-out (ZFS path, default)
sh deploy/prove-on-freebsd.sh

# Or: plaindir backend (no ZFS required, degraded)
JAILRUN_STORE_BACKEND=plaindir sh deploy/prove-on-freebsd.sh

# Or: Tier-2 path (loads linux64)
sh deploy/provision-freebsd.sh --tier2 /dev/vtbd1
sh deploy/prove-on-freebsd.sh
```

`provision-freebsd.sh` is idempotent: it checks `zpool list`, `zfs list`, and
`pkg info` before doing anything, so re-running after a partial failure is safe.

`prove-on-freebsd.sh` is a documented runbook script: each step prints its
intent, runs the real Python store/probe/bakery/runtime code via `PYTHONPATH`,
and stops on first error so failures are pinpointed.

---

## Environment variables summary

| Variable | Default | Purpose |
|---|---|---|
| `JAILRUN_STORE_BACKEND` | `zfs` | `zfs` or `plaindir` |
| `JAILRUN_ZPOOL` | `jailrun` | ZFS pool name |
| `JAILRUN_SRC` | `/mnt/jailrun` | Path to jailrun source on the VM (if using 9p) |
| `JAILRUN_LIFECYCLED_SOCK` | `/var/run/bsdos-lifecycle.sock` | bsdos_lifecycled socket |

---

## Findings — prove-out on FreeBSD 15.1, 2026-07-04

Resolved from the original open items:

1. `skopeo copy --override-os linux docker://busybox oci:…` — **works** on FreeBSD 15.1.
2. `umoci raw unpack` — **`umoci` is not in FreeBSD ports.** Unpack OCI layers with
   **`bsdtar -xpf <layer-blob> -C <rootfs>`** instead (`store.py` already has the bsdtar path;
   treat `umoci` as optional / not-present).
3. **Linux OCI binary under Linuxulator in a jail — works.** `kldload linux64` +
   `jail -c path=<rootfs> …` + `jexec … /bin/busybox` runs a glibc Linux binary; `uname`
   inside → `Linux x86_64`. A self-contained image (its own `/lib64/ld-linux…` + glibc)
   needs no separate `linux_base`.
4. **Native path (no Linuxulator) — works.** `pkg install devel/xtensa-esp-elf` yields a
   **FreeBSD-ELF** `xtensa-esp32-elf-gcc`; esp-idf builds ESP32 firmware with it, zero Linux ABI.

Still to confirm on a real run: `mount_nullfs` file-level bind; `zfs clone` atomicity under
concurrent clones; `zfs destroy` "busy" vs `jail -r` quiesce ordering; musl (Alpine) under
Linuxulator (busybox/glibc was used here).

## Operational gotchas (hard-won)

- **The VM rootfs is small and fills up.** ESP/OCI builds stage to `/tmp`, `/usr/local`,
  `/usr/obj` and can hit 100% on `/`. Keep ALL build output on the `jailrun` pool:
  symlink `~/.platformio` and `~/.espressif` into `/jailrun`, and export `TMPDIR=/jailrun/tmp`,
  `PLATFORMIO_CORE_DIR`, `IDF_TOOLS_PATH`, `WRKDIRPREFIX=/jailrun/obj`. Watch `df -h /`.

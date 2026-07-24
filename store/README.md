# store/ — jailrun S3: ZFS-native OCI store

Implements **Seam 2** (Store API) from `ARCHITECTURE.md`.  All FreeBSD-specific
calls are validated end-to-end on FreeBSD 15.1 (soak 100×8-way, adversarial
layer corpus, ESP32 firmware build).

---

## ZFS ↔ OCI mapping

| OCI concept | ZFS concept | Example |
|---|---|---|
| Image (read-only rootfs) | ZFS dataset + snapshot `@snap` | `zroot/jailrun/images/<image_id>@snap` |
| Container (writable layer) | ZFS clone of that snapshot | `zroot/jailrun/runs/<run_id>` |
| Native base (bakery output) | ZFS dataset + snapshot `@snap` | `zroot/jailrun/bases/<name>-<recipe_hash16>@snap` |
| Bind mount (`-v host:dest:ro`) | nullfs mount on clone mountpoint | `mount_nullfs -o ro /host/path /var/jailrun/runs/<run_id>/dest` |

### Dataset hierarchy

```
zroot/
  jailrun/
    images/
      <image_id>/           # dataset, readonly=on after unpack
        @snap               # immutable; source for all clone() calls
    bases/
      <name>-<recipe_hash>/ # dataset, readonly=on after provisioning
        @snap
    runs/
      <run_id>/             # CoW clone, writable, destroyed on destroy()
```

### Content-addressing

**Image ID** = `sha256(sorted(layer_digests).join("\n"))` where `layer_digests`
are the `sha256:<hex>` strings from the OCI manifest's `layers[]` array.

- Stable across re-pulls: same image layers → same ID.
- Short but collision-resistant for the expected dataset count.
- Stored as the ZFS dataset name (64 hex chars fit within ZFS 256-char limit).

**Base ID** = `sha256(provision_cmd)` (first 16 hex chars used in dataset name
to keep paths readable, full hash logged for audit).

---

## Skopeo on FreeBSD

`sysutils/skopeo` is available in the FreeBSD ports tree.  The critical flag
when pulling Linux images from a FreeBSD host is:

```sh
skopeo copy --override-os linux docker://alpine:3.19 oci:/var/cache/jailrun/oci/alpine_3.19:latest
```

Without `--override-os linux`, skopeo requests a FreeBSD manifest from the
registry index.  Most public registries (Docker Hub, GHCR) do not publish
FreeBSD images, so the copy fails with a "no image found" error.

**Reference:** [skopeo-copy(1) on FreeBSD](https://man.freebsd.org/cgi/man.cgi?query=skopeo-copy&sektion=1&manpath=FreeBSD+13.2-RELEASE+and+Ports),
[skopeo GitHub docs](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md)

---

## Layer unpacking: umoci vs bsdtar

### Preferred: umoci (`sysutils/umoci`)

```sh
umoci raw unpack --image /var/cache/jailrun/oci/alpine_3.19:latest /var/jailrun/images/<id>
```

umoci is the OCI-spec reference implementation for unpacking.  It correctly
handles:

- **Layered application** (lower → upper in manifest order)
- **File whiteouts** (`.wh.<name>` → delete target in lower layer)
- **Opaque whiteouts** (`.wh..wh..opq` → clear all siblings in lower layer
  before applying the current layer's directory content)
- Hardlinks, symlinks, xattrs, device nodes (when running as root)

**FreeBSD support:** umoci compiles and appears to work on FreeBSD per upstream
changelog (v0.4.5+), with the caveat that it currently refuses to extract
non-Linux images on any platform — which is fine for jailrun since we always
pull `--override-os linux`.  (Validated: skopeo pulls work on FreeBSD 15.1.)

**Reference:** [umoci releases](https://github.com/opencontainers/umoci/releases),
[umoci raw-unpack man page](https://manpages.ubuntu.com/manpages/jammy/man1/umoci-raw-unpack.1.html)

### Fallback: bsdtar manual path (`_unpack_bsdtar` in `store.py`)

If umoci is unavailable, `store.py` implements manual whiteout handling:

1. For each layer blob (gzip-compressed tar at `blobs/sha256/<hex>`):
   a. Extract to a temp directory with `bsdtar -xf <blob> -C <tmp>`.
   b. Apply opaque whiteouts **first**: find all `.wh..wh..opq` files; for
      each, delete all existing siblings in the destination rootfs.
   c. Apply file whiteouts: find all `.wh.<name>` files; delete the target
      in the destination rootfs.
   d. Merge the remaining (non-whiteout) files into the rootfs.
2. Scrub any residual `.wh.*` files from the assembled rootfs.

**Gotchas with bsdtar:**
- bsdtar does not natively understand OCI whiteout semantics — the manual step
  is required.
- Device node creation (some images create `/dev/null` etc. in layers) requires
  root and `kern.securelevel <= 0`.
- xattr support depends on how libarchive was built on the FreeBSD port.
- The merge step uses Python `shutil.copy2` which preserves mtime but not
  ACLs or xattrs.  For images that rely on xattrs (capabilities, SELinux
  labels) this is a gap.

**Recommendation:** Install umoci; use bsdtar only as a last resort.

**Reference:** [OCI image-spec layer.md](https://github.com/opencontainers/image-spec/blob/main/layer.md),
[whiteout explanation](https://www.madebymikal.com/interpreting-whiteout-files-in-docker-image-layers/)

---

## ZFS clone/snapshot semantics for ephemeral rootfs

```sh
# Snapshot the unpacked image (instant, zero-copy):
zfs snapshot zroot/jailrun/images/<id>@snap

# Clone for one run (instant, CoW — no data copied until written):
zfs clone zroot/jailrun/images/<id>@snap zroot/jailrun/runs/<run_id>

# Destroy clone after run (reclaims only blocks unique to the clone):
zfs destroy zroot/jailrun/runs/<run_id>
```

Key properties:
- **Clone creation is O(1)** — shares all blocks with the snapshot.
- **Clone storage cost = writes made during the run** only.
- **You cannot destroy a snapshot while clones exist** — the snapshot must
  outlive all its clones.  Destroying all clones first is mandatory, which
  `store.destroy()` enforces.
- `zfs promote` can be used to reverse the parent-child relationship if needed
  (e.g., to "bake" a modified clone into the new base image), but jailrun does
  not use this in the current design.
- **`readonly=on`** on the image dataset does NOT prevent writes from inside a
  jail that has the *clone* (not the dataset) as its rootfs — the clone is
  writable.  It only blocks writes to the original dataset mount.
  (Validated: soak test exercises this exact pattern.)

**Reference:** [FreeBSD ZFS handbook](https://docs.freebsd.org/en/books/handbook/zfs/),
[ZFS clones with jails forum](https://forums.freebsd.org/threads/using-zfs-clone-with-jails.39442/)

---

## mount_nullfs flags

```sh
# Read-only bind mount:
mount_nullfs -o ro /host/src /var/jailrun/runs/<run_id>/dest

# Read-write bind mount:
mount_nullfs /host/src /var/jailrun/runs/<run_id>/dest

# Unmount:
umount /var/jailrun/runs/<run_id>/dest
```

Gotchas:
- **Must be done outside the jail** (before `jail -c`) — mounts issued inside
  a jail require `allow.mount` and `allow.mount.nullfs` jail parameters, which
  escalate privileges.  jailrun mounts from the host before starting the jail.
- **No uid/gid remapping** — host uid numbers appear unchanged inside the jail.
  A file owned by host uid 1000 will appear as uid 1000 inside the jail.
  Document this in `jailrun run` help; do not silently remap.
- **nullfs does not traverse filesystem boundaries** on the source side.  If
  the host path is itself a nullfs mount, this may or may not work depending
  on the FreeBSD version.
- **File-level bind mounts** (e.g., `/etc/resolv.conf`): some versions of
  `mount_nullfs` require the destination to already exist as a file (not a
  directory).  Create the stub file before mounting.
- `noatime` is recommended on busy read-only mounts to avoid write amplification
  on the source: `-o ro,noatime`.

**Reference:** [FreeBSD Forums nullfs ro](https://forums.freebsd.org/threads/how-to-mount-umount-nullfs-ro-at-the-command-line.64093/),
[nullfs jail fstab](https://srobb.net/nullfsjail.html)

---

## Resolved questions (freebsd-host prove-out)

All questions below were open during the design phase and have since been
resolved through soak testing (100×8-way), the adversarial layer corpus,
and the ESP32 firmware build on FreeBSD 15.1.

1. **umoci on FreeBSD**: umoci is **not** in FreeBSD ports. bsdtar
   (`_unpack_bsdtar`) is the primary and only unpack path, with manual
   whiteout handling.  This is a non-issue in practice.

2. **File-level nullfs bind**: `mount_nullfs -o ro /etc/resolv.conf
   <rootfs>/etc/resolv.conf` works when the destination file pre-exists.
   jailrun creates stub files before mounting (validated in soak + adversarial
   tests).

3. **`readonly=on` vs jail writes**: `zfs set readonly=on <image_ds>` does NOT
   block writes from a jail whose rootfs is a clone — the clone is independent.
   Confirmed in soak (every run clones a readonly snapshot).

4. **Device nodes in layers**: bsdtar extracts device nodes as root without
   error. Tested in `store/test_special_file_handling.py`.

5. **Linuxulator + nullfs interaction**: nullfs binds work alongside
   linprocfs in the same jail (validated in the `--network allow` live test).

6. **`zfs clone` mountpoint**: `zfs clone -o mountpoint=<path>` sets the
   mountpoint atomically. Concurrent clone races tested in
   `store/test_store_concurrency.py`.

7. **Concurrent destroy**: `zfs destroy` fails with "dataset is busy" if the
   jail hasn't fully quiesced. `store.destroy()` does `jail -r` first, then
   bounded-retry `zfs destroy` with force from attempt 2. Validated in soak.

8. **Alpine libc vs Linuxulator**: Alpine (musl libc) under Linuxulator works
   for TCP/UDP but needs explicit `/etc/resolv.conf` for DNS. Known limitation,
   documented in THREAT-MODEL.md.

---

## Files

| File | Purpose |
|---|---|
| `store.py` | Python Store class — Seam 2 implementation |
| `store.freebsd.sh` | Shell prove-out script for freebsd-host (FreeBSD) |
| `__init__.py` | Package marker |
| `README.md` | This file |

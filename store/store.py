#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: store/store.py
# PURPOSE: ZFS-native OCI image store implementing Seam 2 (Store API) of jailrun
# INTENT: Provides pull/unpack/clone/mount/destroy lifecycle for OCI container images
#         so the runtime (S1) obtains a writable rootfs clone without knowing whether
#         ZFS CoW or plain directory copies are used underneath.
# DEPENDENCIES: stdlib (hashlib, json, logging, os, re, shlex, shutil, subprocess,
#               tempfile, uuid, dataclasses, pathlib, typing);
#               external tools: skopeo (sysutils/skopeo), umoci (sysutils/umoci),
#               bsdtar (base), zfs/zpool (base), mount_nullfs (base), umount (base),
#               jail/jail -r (base), cp (base)
# PUBLIC_API: Store (class) — resolve(), unpack(), register_base(), base_mountpoint(),
#             clone(), mount(), unmount(), destroy();
#             Handle (dataclass) — opaque clone descriptor;
#             StoreError, ImageNotFoundError, SnapshotNotFoundError (exceptions)
# END_AI_HEADER
# START_INVARIANTS
# - Every clone() produces a unique run_id (uuid4.hex); no two handles share a dataset.
# - ZFS backend: images/<id>@snap is immutable (readonly=on) before clone() is called.
# - plaindir backend: .jailrun_snap sentinel file marks a directory as "snapshotted"
#   (idempotency guard); it is removed from the working copy after cp -a clone.
# - handle.mounts is always cleared by unmount(); destroy() calls unmount() first.
# - sideEffects in every public method name the exact subprocess or filesystem op used.
# END_INVARIANTS
# START_RATIONALE
# Q: Why content-address by sha256(sorted(layer_digests)) rather than the image digest?
# A: The OCI image index digest changes on re-push; layer digests are content-stable.
#    Sorting makes the id reproducible regardless of manifest ordering variation.
# Q: Why --override-os linux in skopeo copy?
# A: FreeBSD hosts request a FreeBSD-platform manifest; most registries do not carry
#    one, so the pull fails.  Forcing linux pulls the widely-available linux manifest.
# Q: Why umoci preferred over bsdtar for layer unpacking?
# A: umoci handles OCI whiteouts (.wh. and .wh..wh..opq) spec-correctly including
#    device nodes and xattrs.  The bsdtar fallback exists for environments where
#    umoci is unavailable; it requires manual whiteout post-processing.
# END_RATIONALE
"""
store.py — jailrun S3: OCI image store with pluggable backend.

Implements Seam 2 (Store API) from ARCHITECTURE.md.  Every public method
either:
  • works on Linux/linux-host (path ops, hashing, subprocess scaffolding), or
  • is clearly marked  # UNVERIFIED  at the line that needs FreeBSD.

Backend selection
-----------------
Set  JAILRUN_STORE_BACKEND  environment variable:

  zfs       (default, production)
            ZFS clone/snapshot for all CoW operations.  Requires a ZFS pool.
            Pool name from  JAILRUN_ZPOOL  (default "jailrun").
            Dataset layout:  <zpool>/images | <zpool>/bases | <zpool>/runs

  plaindir  (degraded, no CoW)
            Plain directory copies instead of ZFS clone/snapshot.
            Suitable for hosts WITHOUT a ZFS pool (CI, local dev, testing).
            cp -a replaces zfs clone; mkdir replaces zfs create; rm -rf
            replaces zfs destroy.  No snapshot semantics.

Pool name
---------
JAILRUN_ZPOOL (default "jailrun") controls the ZFS pool name.
Old layout used  <pool>/jailrun/…  which would produce "jailrun/jailrun" when
pool=jailrun.  Layout is now  <pool>/images | <pool>/bases | <pool>/runs
so JAILRUN_ZPOOL=jailrun → jailrun/images (clean, no double-name).

Design decisions (see README.md for rationale):
  • skopeo copy docker://… oci:<oci_dir>:<tag>  with --override-os linux
  • umoci raw unpack --image <oci_dir>:<tag> <rootfs_dir>  for layer application
    (handles whiteouts spec-compliant; fallback to bsdtar with manual whiteout
    post-processing is in _apply_layers_bsdtar for environments without umoci)
  • ZFS dataset layout:
      <zpool>/images/<image_id>          read-only base dataset
      <zpool>/images/<image_id>@snap     immutable snapshot  ("image")
      <zpool>/bases/<base_name>@snap     native-provisioned bases
      <zpool>/runs/<handle_id>           CoW clone per `clone()` call
  • content-addressing: image_id = sha256 of sorted(layer_digests).hexdigest()
    (stable across re-pulls of identical image); bases keyed by sha256(recipe).

subprocess conventions:
  • _run()   → raises StoreError on non-zero returncode; logs stderr on failure.
  • _run_ok()→ tolerates non-zero when the caller wants to inspect returncode.

FreeBSD tool assumptions (pkg install):
  • skopeo     — sysutils/skopeo
  • umoci      — sysutils/umoci  (preferred; see README for whiteout caveat)
  • zfs / zpool— base system
  • jail / jexec / jail -r  — base system
  • mount_nullfs— base system
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("jailrun.store")

# ---------------------------------------------------------------------------
# Backend constants
# ---------------------------------------------------------------------------

BACKEND_ZFS      = "zfs"
BACKEND_PLAINDIR = "plaindir"
_VALID_BACKENDS  = (BACKEND_ZFS, BACKEND_PLAINDIR)

# ---------------------------------------------------------------------------
# Subprocess timeout tiers — a hung fetch or wedged build must
# be killable, not hang forever. Sized generously and overridable via env; these
# are provisional until profiled against a real esphome/platformio provision
# (re-verify together with a real compile).
# ---------------------------------------------------------------------------
# Fast local ops: zfs create/snapshot/destroy, mount_nullfs, umount, jail -r, zfs list.
DEFAULT_LOCAL_TIMEOUT_S = float(os.environ.get("JAILRUN_LOCAL_TIMEOUT_S", "60"))
# Network fetches: skopeo pull of an OCI image.
DEFAULT_NETWORK_TIMEOUT_S = float(os.environ.get("JAILRUN_NETWORK_TIMEOUT_S", "600"))
# Layer extraction: umoci/bsdtar unpacking a (potentially large) image's layers.
DEFAULT_EXTRACT_TIMEOUT_S = float(os.environ.get("JAILRUN_EXTRACT_TIMEOUT_S", "300"))
# Base provisioning: register_base()'s provision_cmd — pkg install is fast, but a
# port BUILD (e.g. devel/xtensa-esp-elf) can legitimately take a long time.
DEFAULT_PROVISION_TIMEOUT_S = float(os.environ.get("JAILRUN_PROVISION_TIMEOUT_S", "3600"))


# _get_backend: reads JAILRUN_STORE_BACKEND env var, validates against _VALID_BACKENDS, raises StoreError on unknown value
def _get_backend() -> str:
    """Return the active backend from JAILRUN_STORE_BACKEND; default zfs."""
    val = os.environ.get("JAILRUN_STORE_BACKEND", BACKEND_ZFS).strip().lower()
    if val not in _VALID_BACKENDS:
        raise StoreError(
            f"Unknown JAILRUN_STORE_BACKEND={val!r}; valid: {_VALID_BACKENDS}"
        )
    return val


# _get_zpool: reads JAILRUN_ZPOOL env var, returns string pool name (no validation; pool existence checked at ZFS op time)
def _get_zpool() -> str:
    """Return ZFS pool name from JAILRUN_ZPOOL; default 'jailrun'."""
    return os.environ.get("JAILRUN_ZPOOL", "jailrun").strip()


# _get_mountpoint_base: reads JAILRUN_MOUNTPOINT_BASE env var, returns plaindir
# tree root (no validation; existence/writability checked at op time).
# Needed when jailrun itself runs inside a jail that nullfs-binds a host
# /var/jailrun in: nullfs cannot mount onto a path that is itself already
# inside a nullfs mount (FreeBSD returns EDEADLK, "Resource deadlock
# avoided") -- so a jailrun-in-jail deployment must point its own
# images/bases/runs tree at a directory that is NATIVE to that jail's own
# filesystem, not another nullfs bind, or every -v bind mount into a nested
# run's rootfs (which lives under mountpoint_base) fails.
def _get_mountpoint_base() -> str:
    """Return the plaindir/ZFS-mountpoint tree root from JAILRUN_MOUNTPOINT_BASE; default '/var/jailrun'."""
    return os.environ.get("JAILRUN_MOUNTPOINT_BASE", "/var/jailrun").strip()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StoreError(RuntimeError):
    """Raised for any store-layer failure."""


class ImageNotFoundError(StoreError):
    """Raised when an image_id is not present in the local OCI cache."""


class SnapshotNotFoundError(StoreError):
    """Raised when a requested snapshot does not exist."""


# ---------------------------------------------------------------------------
# Handle — public CoW-clone descriptor returned by clone()
# ---------------------------------------------------------------------------

# Handle:start
#   purpose: opaque CoW-clone descriptor carrying all state needed for mount/unmount/destroy
#   input:
#     id: str — uuid4.hex run identifier, unique per clone
#     rootfs: Path — ZFS mountpoint or plaindir directory where the writable rootfs lives
#     dataset: str — full ZFS dataset name (ZFS backend) or plaindir path (plaindir backend)
#     snapshot_id: str — source snapshot_id this clone was derived from
#     mounts: list[tuple[Path, Path, bool]] — accumulated nullfs mounts (host, dest, readonly)
#     jail_name: Optional[str] — populated by caller if a jail was started for this handle
#   output:
#     instance: Handle
#   sideEffects: none
#   rationale: kept as a plain dataclass (not a class with methods) so Store owns all
#              lifecycle logic; callers treat Handle as an opaque token
@dataclass
class Handle:
    """
    Opaque descriptor for one ephemeral rootfs clone.  Returned by Store.clone();
    passed to Store.mount(), Store.unmount(), Store.destroy().

    Fields are internal to the store — callers should treat this as opaque.
    """

    id: str                              # unique run ID (UUID4, no hyphens)
    rootfs: Path                         # ZFS mountpoint (or plaindir path) of the clone
    dataset: str                         # full ZFS dataset name OR plaindir path
    snapshot_id: str                     # source snapshot_id this was cloned from
    mounts: list[tuple[Path, Path, bool]] = field(default_factory=list)
    # mounts entries: (host_path, dest_inside_rootfs, is_readonly)
    jail_name: Optional[str] = None      # set if a jail was started for this handle

    # rootfs_str: returns str(self.rootfs) (pure, no IO)
    @property
    def rootfs_str(self) -> str:
        return str(self.rootfs)
# Handle:end


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

# Store:start
#   purpose: OCI image store for jailrun (Seam 2 / S3) — ZFS-native CoW by default,
#            plain directory fallback for hosts without a ZFS pool
#   intent: single class that owns all storage lifecycle: pull (skopeo), unpack (umoci/bsdtar),
#           snapshot (zfs snapshot / sentinel file), clone (zfs clone / cp -a),
#           nullfs bind-mount, unmount, and destroy
#   sideEffects: all effectful operations delegated to public and private methods;
#                __init__ itself only reads env vars and creates Path objects (no IO)
class Store:
    """
    OCI image store for jailrun (S3) — ZFS-native by default, plaindir fallback.

    Parameters
    ----------
    pool : str | None
        ZFS pool name.  Defaults to JAILRUN_ZPOOL env (default "jailrun").
        Ignored when backend is plaindir.
    oci_cache_dir : str | Path
        Host directory used as OCI image layout cache (skopeo destination).
        Defaults to  /var/cache/jailrun/oci .
    mountpoint_base : str | Path
        Host directory where ZFS datasets / plaindir trees are mounted.
        Defaults to  /var/jailrun .
    umoci : bool
        If True, use umoci for layer unpacking (correct whiteout handling).
        Default False: confirmed live 2026-07-04/2026-07-19 that umoci is NOT
        packaged for FreeBSD (sysutils/umoci does not exist in ports) — the
        bsdtar manual path is what actually runs on jailrun's only real target
        platform, and it carries its own hardened whiteout/symlink-escape
        handling (see _merge_tree, _clear_opaque_whiteout, _apply_file_whiteout).
        Set True only on a host where umoci is confirmed installed.
    backend : str | None
        Override backend selection; if None, reads JAILRUN_STORE_BACKEND.

    Dataset layout (ZFS, pool="jailrun")
    ------------------------------------
        jailrun/images/<image_id>        read-only dataset
        jailrun/images/<image_id>@snap   immutable snapshot
        jailrun/bases/<name>-<hash>@snap native bases (bakery)
        jailrun/runs/<run_id>            ephemeral CoW clone

    Directory layout (plaindir, mountpoint_base="/var/jailrun")
    -----------------------------------------------------------
        /var/jailrun/images/<image_id>/  unpacked rootfs (treated as "snapshot")
        /var/jailrun/bases/<name>-<hash>/
        /var/jailrun/runs/<run_id>/      cp -a copy (no CoW)
    """

    # __init__:start
    #   purpose: initialise Store, resolve backend and ZFS pool name, derive dataset prefixes
    #   input:
    #     pool: str | None — ZFS pool name override; None reads JAILRUN_ZPOOL
    #     oci_cache_dir: str | Path — host directory for skopeo OCI layout cache
    #     mountpoint_base: str | Path | None — host directory root for ZFS
    #       mountpoints / plaindir trees; None reads JAILRUN_MOUNTPOINT_BASE
    #     umoci: bool — True = umoci layer unpacking; False = bsdtar fallback
    #     backend: str | None — backend override; None reads JAILRUN_STORE_BACKEND
    #   output:
    #     none (constructor)
    #   sideEffects: none (reads env vars via _get_backend()/_get_zpool()/
    #                _get_mountpoint_base(); no filesystem ops)
    def __init__(
        self,
        pool: str | None = None,
        oci_cache_dir: str | Path = "/var/cache/jailrun/oci",
        mountpoint_base: str | Path | None = None,
        umoci: bool = False,
        backend: str | None = None,
    ) -> None:
        self.backend = backend if backend is not None else _get_backend()
        if self.backend not in _VALID_BACKENDS:
            raise StoreError(f"Invalid backend: {self.backend!r}")

        self.pool = (pool if pool is not None else _get_zpool()) if self.backend == BACKEND_ZFS else ""
        self.oci_cache_dir = Path(oci_cache_dir)
        self.mountpoint_base = Path(mountpoint_base if mountpoint_base is not None else _get_mountpoint_base())
        self.use_umoci = umoci

        # Derived dataset/dir prefixes — no double-name: pool/images, not pool/jailrun/images
        if self.backend == BACKEND_ZFS:
            self._images_ds = f"{self.pool}/images"
            self._bases_ds  = f"{self.pool}/bases"
            self._runs_ds   = f"{self.pool}/runs"
        else:
            # plaindir: use absolute paths under mountpoint_base
            self._images_ds = str(self.mountpoint_base / "images")
            self._bases_ds  = str(self.mountpoint_base / "bases")
            self._runs_ds   = str(self.mountpoint_base / "runs")

        log.debug(
            "Store init: backend=%s pool=%r images=%s",
            self.backend, self.pool, self._images_ds,
        )
    # __init__:end

    # ------------------------------------------------------------------
    # Public API (Seam 2)
    # ------------------------------------------------------------------

    # resolve:start
    #   purpose: pull an OCI image from a Docker registry into the local OCI layout cache
    #            and return its content-addressed image_id
    #   input:
    #     image_ref: str — Docker image reference, e.g. "debian:bookworm" or "registry/repo:tag"
    #   output:
    #     image_id: str — sha256 hex of sorted layer digests; stable key for unpack()
    #   sideEffects: runs 'skopeo copy --override-os linux docker://<image_ref>
    #                oci:<oci_cache_dir>/<safe_name>:latest' (network + disk write);
    #                creates oci_cache_dir/<safe_name>/ directory if absent
    def resolve(self, image_ref: str) -> str:
        """
        Ensure ``image_ref`` is present in the local OCI cache and return its
        content-addressed ``image_id`` (sha256 of ordered layer digests).

        Shells out to::

            skopeo copy \\
                --override-os linux \\
                docker://<image_ref> \\
                oci:<oci_cache_dir>/<safe_name>:latest

        The ``--override-os linux`` flag is essential on FreeBSD: without it
        skopeo would request a FreeBSD manifest which most registries do not
        carry, and the copy fails.   # UNVERIFIED (only relevant on FreeBSD)

        Returns
        -------
        str
            ``image_id`` suitable for passing to ``unpack()``.
        """
        oci_dir = self._oci_dir_for(image_ref)
        oci_dir.mkdir(parents=True, exist_ok=True)

        tag = "latest"
        oci_dest = f"oci:{oci_dir}:{tag}"

        log.info("resolve: pulling %s -> %s", image_ref, oci_dest)
        self._run([
            "skopeo", "copy",
            "--override-os", "linux",   # UNVERIFIED: FreeBSD needs this flag
            f"docker://{image_ref}",
            oci_dest,
        ], timeout=DEFAULT_NETWORK_TIMEOUT_S)

        image_id = self._compute_image_id(oci_dir, tag)
        log.info("resolve: image_id=%s", image_id)
        return image_id
    # resolve:end

    # unpack:start
    #   purpose: unpack a previously resolved OCI image into a dataset/directory and
    #            mark it as immutable (ZFS snapshot or sentinel file)
    #   input:
    #     image_id: str — content-addressed id returned by resolve()
    #   output:
    #     snapshot_id: str — ZFS: '<images_ds>/<image_id>@snap';
    #                        plaindir: '<images_dir>/<image_id>' (directory path)
    #   sideEffects: ZFS path: runs 'zfs create -o mountpoint=<mp> <dataset>',
    #                           runs umoci or bsdtar layer extraction into mountpoint,
    #                           runs 'zfs snapshot <dataset>@snap',
    #                           runs 'zfs set readonly=on <dataset>';
    #                plaindir: creates directory, runs umoci or bsdtar extraction,
    #                           writes <dest>/.jailrun_snap sentinel file
    #   rationale: idempotent — returns existing snapshot_id if snapshot already exists
    def unpack(self, image_id: str) -> str:
        """
        Unpack a previously resolved image into a dataset/dir and "snapshot" it.

        ZFS path
        --------
        1. Locate the OCI layout dir for this image_id via the index.
        2. Create a ZFS dataset  ``<images_ds>/<image_id>``  if absent.  # UNVERIFIED
        3. Extract all OCI layers in order (umoci preferred; bsdtar fallback).
        4. ``zfs snapshot <dataset>@snap``.                                 # UNVERIFIED
        5. Set the dataset readonly.                                        # UNVERIFIED

        plaindir path
        -------------
        1–3. Same OCI resolution + extraction.
        4. Write a sentinel file ``<dir>/.jailrun_snap`` (marks "snapshottted").
        5. No readonly enforcement (plaindir has no equivalent).

        Returns
        -------
        str
            ``snapshot_id``:
              ZFS:      ``<images_ds>/<image_id>@snap``
              plaindir: ``<images_dir>/<image_id>``  (the directory itself)
        """
        oci_dir, tag = self._find_oci_for_image_id(image_id)

        if self.backend == BACKEND_ZFS:
            return self._unpack_zfs(image_id, oci_dir, tag)
        else:
            return self._unpack_plaindir(image_id, oci_dir, tag)
    # unpack:end

    # register_base:start
    #   purpose: provision a native FreeBSD base into a dataset/dir, snapshot it,
    #            and return its snapshot_id for use as a clone source
    #   input:
    #     name: str — human-readable base name, e.g. "esphome-native-base"
    #     provision_cmd: str — shell command run inside the base mountpoint to install
    #                          packages/ports, e.g. "pkg install -y python311"
    #   output:
    #     snapshot_id: str — ZFS: '<bases_ds>/<name>-<hash16>@snap';
    #                        plaindir: '<bases_dir>/<name>-<hash16>'
    #   sideEffects: ZFS path: runs 'zfs create -o mountpoint=<mp> <dataset>',
    #                           runs 'sh -c <provision_cmd>' with cwd=mountpoint,
    #                           runs 'zfs snapshot <dataset>@snap',
    #                           runs 'zfs set readonly=on <dataset>';
    #                plaindir: creates directory, runs 'sh -c <provision_cmd>' with cwd=dest,
    #                           writes <dest>/.jailrun_snap sentinel file
    #   rationale: keyed by sha256(provision_cmd) so identical recipes are idempotent;
    #              name is sanitised for ZFS dataset naming constraints
    def register_base(self, name: str, provision_cmd: str) -> str:
        """
        Provision a native FreeBSD base (via pkg/ports), snapshot it, and return
        the snapshot_id.  Used by bakery (S4) to register native artifact sets.

        The ``provision_cmd`` is run as a shell command inside the base dataset's
        mountpoint.  A hash of the command is the key — calling with the same
        ``provision_cmd`` is idempotent.

        Parameters
        ----------
        name : str
            Human-readable name, e.g. "esphome-native-base".
        provision_cmd : str
            Shell command to run that installs packages/ports into the rootfs,
            e.g.  ``"pkg install -y python311 xtensa-esp-elf"``.

        Returns
        -------
        str
            ``snapshot_id``:
              ZFS:      ``<bases_ds>/<name>-<hash16>@snap``
              plaindir: ``<bases_dir>/<name>-<hash16>``
        """
        recipe_hash = _sha256_str(provision_cmd)
        safe_name = _safe_zfs_name(name)
        suffix = f"{safe_name}-{recipe_hash[:16]}"

        if self.backend == BACKEND_ZFS:
            return self._register_base_zfs(suffix, provision_cmd)
        else:
            return self._register_base_plaindir(suffix, provision_cmd)
    # register_base:end

    # base_mountpoint:start
    #   purpose: resolve a register_base()-returned snapshot_id back to the HOST
    #            directory where that base's provisioned artifacts actually live
    #   input:
    #     snapshot_id: str — value previously returned by register_base()
    #   output:
    #     mountpoint: Path — host directory containing the provisioned base
    #                 (e.g. .../usr/local/bin/python3.11 lives under this path)
    #   sideEffects: none (pure path computation; does not touch ZFS)
    #   rationale: runtime (S1) needs this to bind-mount the base into a per-run
    #              clone — native.artifact_path values (e.g. "/usr/local/bin/
    #              python3.11") are absolute paths ON THE BASE's OWN mountpoint,
    #              not inside any particular image clone, so engine.py must nullfs
    #              the base in before native-shadow symlinks can resolve (this was
    #              previously wired nowhere, so shadow symlinks pointed at a host
    #              path invisible from inside the jail)
    def base_mountpoint(self, snapshot_id: str) -> Path:
        """Return the host directory a register_base() snapshot_id mounts at."""
        if self.backend == BACKEND_ZFS:
            # snapshot_id == "<bases_ds>/<suffix>@snap" -> mountpoint_base/bases/<suffix>
            dataset = snapshot_id.split("@", 1)[0]
            suffix = dataset.rsplit("/", 1)[-1]
            return self.mountpoint_base / "bases" / suffix
        # plaindir: register_base() already returned the directory itself.
        return Path(snapshot_id)
    # base_mountpoint:end

    # base_snapshot:start
    #   purpose: resolve a pre-provisioned NATIVE base by human name to a
    #            clone-source snapshot_id, for use as a run's ROOT rootfs
    #            (the native-first FreeBSD-userland path — no OCI/skopeo, no
    #            Linux shadow, no Linuxulator). Distinct from register_base(),
    #            which is hash-keyed by provision recipe and mounted INTO a run;
    #            this addresses a base by a stable name a caller already knows
    #            (e.g. engine's `nativebase:freebsd-15.1`).
    #   input:
    #     name: str — base name, e.g. "freebsd-15.1"; sanitised like dataset names
    #   output:
    #     snapshot_id: str — ZFS '<bases_ds>/<safe>@snap' | plaindir '<bases_dir>/<safe>'
    #   sideEffects: none (pure lookup); raises SnapshotNotFoundError if absent
    def base_snapshot(self, name: str) -> str:
        """Return the clone-source snapshot_id for a named native base.

        Raises SnapshotNotFoundError if the base has not been provisioned yet
        (create it once with base.txz extraction into <bases_dir>/<name>, or a
        ZFS base dataset + @snap)."""
        safe = _safe_zfs_name(name)
        if self.backend == BACKEND_ZFS:
            snapshot_id = f"{self._bases_ds}/{safe}@snap"
            if not self._zfs_snapshot_exists(snapshot_id):
                raise SnapshotNotFoundError(
                    f"native base not provisioned: {name!r} (expected {snapshot_id})"
                )
            return snapshot_id
        dest = Path(self._bases_ds) / safe
        if not dest.is_dir():
            raise SnapshotNotFoundError(
                f"native base not provisioned: {name!r} (expected {dest})"
            )
        return str(dest)
    # base_snapshot:end

    # clone:start
    #   purpose: create a writable CoW clone of a snapshot for one jail run
    #   input:
    #     snapshot_id: str — ZFS snapshot name (e.g. 'jailrun/images/abc@snap') or
    #                        plaindir path produced by unpack() / register_base()
    #   output:
    #     tuple[Path, Handle] — (rootfs_path, handle); rootfs_path is the clone mountpoint
    #                           or directory; handle is the opaque descriptor for further ops
    #   sideEffects: ZFS path: runs 'zfs clone -o mountpoint=<mp> <snapshot_id> <runs_ds>/<run_id>';
    #                           creates mountpoint directory;
    #                plaindir: runs 'cp -a <src> <runs_dir>/<run_id>';
    #                           removes .jailrun_snap sentinel from clone directory
    def clone(self, snapshot_id: str) -> tuple[Path, Handle]:
        """
        Create a CoW writable clone of ``snapshot_id`` for one jail run.

        ZFS path
        --------
            zfs clone <snapshot_id> <runs_ds>/<run_id>    # UNVERIFIED

        plaindir path
        -------------
            cp -a <snapshot_dir> <runs_dir>/<run_id>
            (no CoW — full copy; degraded mode)

        Returns
        -------
        tuple[Path, Handle]
            ``(rootfs_path, handle)`` — rootfs_path is the clone's mountpoint /
            directory; handle is passed to mount/unmount/destroy.
        """
        if self.backend == BACKEND_ZFS:
            return self._clone_zfs(snapshot_id)
        else:
            return self._clone_plaindir(snapshot_id)
    # clone:end

    # mount:start
    #   purpose: bind-mount host paths into the clone rootfs using nullfs
    #   input:
    #     handle: Handle — clone descriptor whose rootfs receives the mounts
    #     binds: list[tuple[str|Path, str|Path, bool]] | None — list of
    #            (host_path, dest_inside_container, is_readonly) tuples; None = no-op
    #   output:
    #     none
    #   sideEffects: for each bind: creates dest directory inside rootfs if absent
    #                (Path.mkdir); runs 'mount_nullfs [-o ro] <host_path> <dest_path>';
    #                appends (host_path, dest_path, readonly) to handle.mounts
    def mount(
        self,
        handle: Handle,
        binds: list[tuple[str | Path, str | Path, bool]] | None = None,
    ) -> None:
        """
        Bind-mount host paths into the clone rootfs.

        ZFS path: uses mount_nullfs.         # UNVERIFIED
        plaindir path: also uses mount_nullfs on FreeBSD; same code path.

        Parameters
        ----------
        handle : Handle
        binds : list of (host_path, dest_inside_container, is_readonly)
            ``dest_inside_container`` is relative to rootfs; a leading ``/``
            is stripped and the path is joined under ``handle.rootfs``.

        Shells out to::

            mount_nullfs [-o ro] <host_path> <rootfs/dest>   # UNVERIFIED

        Notes
        -----
        • mount_nullfs does not remap uid/gid — host uids appear unchanged
          inside the jail.  This is a known limitation; document in README.
        • Mounts must be performed *outside* the jail (before jail -c).
        • mount_nullfs does not traverse filesystem boundaries on the host side;
          the host_path must be a real directory (not itself inside a nullfs mount
          on some implementations).
        """
        if binds is None:
            return

        rootfs_real = handle.rootfs.resolve()
        for host_raw, dest_raw, readonly in binds:
            host_path = Path(host_raw)
            dest_rel = str(dest_raw).lstrip("/")
            dest_path = handle.rootfs / dest_rel
            # SECURITY: dest_rel comes from `-v host:ctr` — normally the
            # operator's own input, but a caller may build it from data derived
            # from an untrusted user upload (e.g. a component name). A `../../etc` component
            # would otherwise land outside the rootfs entirely once mkdir/mount_nullfs act on
            # it. _within() handles not-yet-existing paths correctly (see its own docstring).
            if not _within(dest_path, rootfs_real):
                raise StoreError(
                    f"mount destination {dest_raw!r} resolves outside the rootfs "
                    f"({dest_path} not under {rootfs_real}); refusing to mount"
                )
            dest_path.mkdir(parents=True, exist_ok=True)

            cmd = ["mount_nullfs"]             # UNVERIFIED: FreeBSD mount_nullfs
            if readonly:
                cmd += ["-o", "ro"]
            cmd += [str(host_path), str(dest_path)]

            log.info("mount: %s -> %s (ro=%s)", host_path, dest_path, readonly)
            self._run(cmd)                     # UNVERIFIED

            handle.mounts.append((host_path, dest_path, readonly))
    # mount:end

    # unmount:start
    #   purpose: unmount all nullfs bind-mounts for a handle in reverse mount order
    #   input:
    #     handle: Handle — clone descriptor whose handle.mounts list is drained
    #   output:
    #     none
    #   sideEffects: for each mount in reverse order: runs 'umount <dest_path>'
    #                (tolerates failure via _run_ok); clears handle.mounts list
    def unmount(self, handle: Handle) -> None:
        """
        Unmount all nullfs mounts associated with ``handle`` in reverse order.

        Shells out to::

            umount <dest_path>                  # UNVERIFIED
        """
        for host_path, dest_path, _ro in reversed(handle.mounts):
            log.info("unmount: %s", dest_path)
            self._run_ok(["umount", str(dest_path)])    # UNVERIFIED
        handle.mounts.clear()
    # unmount:end

    # destroy:start
    #   purpose: fully tear down a clone — stop jail if running, unmount binds, destroy storage
    #   input:
    #     handle: Handle — clone descriptor to destroy
    #   output:
    #     none
    #   sideEffects: if handle.jail_name set: runs 'jail -r <jail_name>' (tolerates failure);
    #                calls self.unmount(handle) which runs 'umount' for each bind;
    #                ZFS path: runs 'zfs destroy <dataset>'; attempts handle.rootfs.rmdir()
    #                          to remove empty mountpoint stub;
    #                plaindir: calls _rm_rf(handle.rootfs) which runs shutil.rmtree
    def destroy(self, handle: Handle) -> None:
        """
        Tear down a running/stopped clone: stop jail if running, unmount nullfs
        binds, destroy the ZFS clone dataset (or rm -rf for plaindir).

        Sequence
        --------
        1. ``jail -r <jail_name>``     if a jail was started               # UNVERIFIED
        2. ``unmount(handle)``         remove nullfs binds
        3. ZFS: ``zfs destroy <dataset>``                                   # UNVERIFIED
           plaindir: ``rm -rf <rootfs>``

        It is safe to call destroy() on a handle that was never jailed or
        never had mounts applied (both steps no-op gracefully).
        """
        # Step 1: stop jail if running
        if handle.jail_name:
            log.info("destroy: stopping jail %s", handle.jail_name)
            self._run_ok(["jail", "-r", handle.jail_name])  # UNVERIFIED

        # Step 2: unmount binds
        self.unmount(handle)

        # Step 3: destroy storage
        if self.backend == BACKEND_ZFS:
            log.info("destroy: zfs destroy %s", handle.dataset)
            # store/README.md open-question #7, confirmed live 2026-07-19: right
            # after `jail -r` returns, the kernel can take a brief moment to fully
            # release the jail's vnode/mount references, so `zfs destroy` can hit
            # "dataset is busy" even though the jail is genuinely gone. A trivial
            # workload (e.g. `id; uname -a`) clears within ~1s; a REAL build
            # (esphome/platformio: ccache/ninja workers, mmap'd toolchain
            # binaries) stayed busy through 10 retries AND -f-on-the-last-3
            # live 2026-07-19 — force from attempt 2 on instead of waiting.
            # This dataset is ephemeral per-run scratch space about to be
            # destroyed anyway, so forcing is safe here (never done for
            # anything persistent).
            last_exc: StoreError | None = None
            attempts = 10
            for attempt in range(attempts):
                try:
                    cmd = ["zfs", "destroy"]
                    if attempt >= 2:
                        cmd.append("-f")
                    cmd.append(handle.dataset)
                    self._run(cmd)
                    last_exc = None
                    break
                except StoreError as exc:
                    last_exc = exc
                    if attempt < attempts - 1:
                        time.sleep(min(1.0 * (attempt + 1), 5.0))
            if last_exc is not None:
                raise last_exc
            # Clean up mountpoint stub if empty
            try:
                handle.rootfs.rmdir()
            except OSError:
                pass
        else:
            log.info("destroy: rm -rf %s", handle.rootfs)
            _rm_rf(handle.rootfs)

        log.info("destroy: handle %s torn down", handle.id)
    # destroy:end

    # ------------------------------------------------------------------
    # Backend implementations — unpack
    # ------------------------------------------------------------------

    # _unpack_zfs:start
    #   purpose: create a ZFS dataset, extract OCI layers into it, snapshot and lock read-only
    #   input:
    #     image_id: str — content-addressed id used as dataset name component
    #     oci_dir: Path — local OCI layout directory produced by resolve()
    #     tag: str — OCI tag to unpack (always "latest" in current scheme)
    #   output:
    #     snapshot_id: str — '<images_ds>/<image_id>@snap'
    #   sideEffects: creates mountpoint directory via Path.mkdir;
    #                runs 'zfs create -o mountpoint=<mp> <dataset>';
    #                runs umoci or bsdtar layer extraction into mountpoint;
    #                runs 'zfs snapshot <dataset>@snap';
    #                runs 'zfs set readonly=on <dataset>'
    #   rationale: idempotent — returns existing snapshot_id when _zfs_snapshot_exists() is true
    def _unpack_zfs(self, image_id: str, oci_dir: Path, tag: str) -> str:
        dataset = f"{self._images_ds}/{image_id}"
        snapshot_id = f"{dataset}@snap"
        mountpoint = self.mountpoint_base / "images" / image_id

        if self._zfs_snapshot_exists(snapshot_id):
            log.info("unpack: snapshot %s already exists, skipping", snapshot_id)
            return snapshot_id

        mountpoint.mkdir(parents=True, exist_ok=True)

        self._run([
            "zfs", "create",
            "-p",  # auto-create parent datasets (e.g. <pool>/images) — nothing else
                   # in jailrun ever creates them; found live 2026-07-19 —
                   # the very first unpack() on a fresh pool failed with
                   # "parent does not exist" without this.
            "-o", f"mountpoint={mountpoint}",
            dataset,
        ])

        rootfs = mountpoint
        if self.use_umoci:
            self._unpack_umoci(oci_dir, tag, rootfs)
        else:
            self._unpack_bsdtar(oci_dir, tag, rootfs)

        self._run(["zfs", "snapshot", snapshot_id])     # UNVERIFIED
        self._run([                                      # UNVERIFIED
            "zfs", "set", "readonly=on", dataset,
        ])

        log.info("unpack: created snapshot %s", snapshot_id)
        return snapshot_id
    # _unpack_zfs:end

    # _unpack_plaindir:start
    #   purpose: extract OCI layers into a plain directory and write a sentinel to mark completion
    #   input:
    #     image_id: str — content-addressed id used as directory name
    #     oci_dir: Path — local OCI layout directory
    #     tag: str — OCI tag to unpack
    #   output:
    #     snapshot_id: str — absolute path str of the unpacked directory
    #   sideEffects: creates dest directory via Path.mkdir; runs umoci or bsdtar extraction;
    #                writes <dest>/.jailrun_snap sentinel file via Path.write_text
    #   rationale: idempotent — returns early when sentinel exists
    def _unpack_plaindir(self, image_id: str, oci_dir: Path, tag: str) -> str:
        images_dir = Path(self._images_ds)
        dest = images_dir / image_id
        sentinel = dest / ".jailrun_snap"

        if sentinel.exists():
            log.info("unpack(plaindir): %s already unpacked, skipping", dest)
            return str(dest)

        dest.mkdir(parents=True, exist_ok=True)

        if self.use_umoci:
            self._unpack_umoci(oci_dir, tag, dest)
        else:
            self._unpack_bsdtar(oci_dir, tag, dest)

        # Write sentinel so idempotent re-runs skip re-extraction
        sentinel.write_text(image_id)

        log.info("unpack(plaindir): unpacked to %s", dest)
        return str(dest)
    # _unpack_plaindir:end

    # ------------------------------------------------------------------
    # Backend implementations — register_base
    # ------------------------------------------------------------------

    # _register_base_zfs:start
    #   purpose: create a ZFS dataset for a native base, provision it, snapshot and lock
    #   input:
    #     suffix: str — sanitised dataset name component '<safe_name>-<hash16>'
    #     provision_cmd: str — shell command to install packages/ports into the mountpoint
    #   output:
    #     snapshot_id: str — '<bases_ds>/<suffix>@snap'
    #   sideEffects: creates mountpoint directory via Path.mkdir;
    #                runs 'zfs create -o mountpoint=<mp> <dataset>';
    #                runs 'sh -c <provision_cmd>' with cwd=mountpoint;
    #                runs 'zfs snapshot <dataset>@snap';
    #                runs 'zfs set readonly=on <dataset>'
    #   rationale: idempotent — returns existing snapshot_id when _zfs_snapshot_exists() is true
    def _register_base_zfs(self, suffix: str, provision_cmd: str) -> str:
        dataset = f"{self._bases_ds}/{suffix}"
        snapshot_id = f"{dataset}@snap"
        mountpoint = self.mountpoint_base / "bases" / suffix

        if self._zfs_snapshot_exists(snapshot_id):
            log.info("register_base: snapshot %s already exists", snapshot_id)
            return snapshot_id

        mountpoint.mkdir(parents=True, exist_ok=True)

        self._run([
            "zfs", "create",
            "-p",  # auto-create parent datasets (e.g. <pool>/bases) — same fix as unpack()
            "-o", f"mountpoint={mountpoint}",
            dataset,
        ])

        _seed_pkg_trust_keys(mountpoint)

        log.info("register_base: provisioning into %s", mountpoint)
        # JAILRUN_BASE_ROOT lets provision_cmd (plan_to_provision_cmd's rendered
        # `pkg -r "$JAILRUN_BASE_ROOT" install ...`) target the isolated base
        # dataset. Fixed 2026-07-19: plain `pkg install` with
        # only cwd=mountpoint installs onto the LIVE HOST's real system (cwd does
        # not redirect pkg's install root) — confirmed live the first time this
        # ever ran for a manifest needing pkg installs; this is exactly the
        # "chroot or pkgbase logic" the old comment here already anticipated.
        self._run(
            ["sh", "-c", provision_cmd],
            cwd=str(mountpoint),
            timeout=DEFAULT_PROVISION_TIMEOUT_S,
            env={"JAILRUN_BASE_ROOT": str(mountpoint)},
        )

        self._run(["zfs", "snapshot", snapshot_id])   # UNVERIFIED
        self._run(["zfs", "set", "readonly=on", dataset])  # UNVERIFIED

        log.info("register_base: snapshot %s registered", snapshot_id)
        return snapshot_id
    # _register_base_zfs:end

    # _register_base_plaindir:start
    #   purpose: create a plain directory for a native base, provision it, write sentinel
    #   input:
    #     suffix: str — sanitised directory name component '<safe_name>-<hash16>'
    #     provision_cmd: str — shell command to install packages/ports into the directory
    #   output:
    #     snapshot_id: str — absolute path str of the base directory
    #   sideEffects: creates dest directory via Path.mkdir;
    #                runs 'sh -c <provision_cmd>' with cwd=dest;
    #                writes <dest>/.jailrun_snap sentinel file via Path.write_text
    #   rationale: idempotent — returns early when sentinel exists
    def _register_base_plaindir(self, suffix: str, provision_cmd: str) -> str:
        bases_dir = Path(self._bases_ds)
        dest = bases_dir / suffix
        sentinel = dest / ".jailrun_snap"

        if sentinel.exists():
            log.info("register_base(plaindir): %s already exists", dest)
            return str(dest)

        dest.mkdir(parents=True, exist_ok=True)
        _seed_pkg_trust_keys(dest)

        log.info("register_base(plaindir): provisioning into %s", dest)
        self._run(
            ["sh", "-c", provision_cmd],
            cwd=str(dest),
            timeout=DEFAULT_PROVISION_TIMEOUT_S,
            env={"JAILRUN_BASE_ROOT": str(dest)},  # same fix as the ZFS path, see above
        )

        sentinel.write_text(suffix)
        log.info("register_base(plaindir): base at %s", dest)
        return str(dest)
    # _register_base_plaindir:end

    # ------------------------------------------------------------------
    # Backend implementations — clone
    # ------------------------------------------------------------------

    # _clone_zfs:start
    #   purpose: create a writable ZFS CoW clone of a snapshot for one run
    #   input:
    #     snapshot_id: str — full ZFS snapshot name, e.g. 'jailrun/images/abc@snap'
    #   output:
    #     tuple[Path, Handle] — (mountpoint Path, Handle with id/rootfs/dataset/snapshot_id)
    #   sideEffects: creates mountpoint directory via Path.mkdir;
    #                runs 'zfs clone -o mountpoint=<mp> <snapshot_id> <runs_ds>/<run_id>'
    def _clone_zfs(self, snapshot_id: str) -> tuple[Path, Handle]:
        if not self._zfs_snapshot_exists(snapshot_id):
            raise SnapshotNotFoundError(f"snapshot not found: {snapshot_id}")

        run_id = uuid.uuid4().hex
        clone_dataset = f"{self._runs_ds}/{run_id}"
        mountpoint = self.mountpoint_base / "runs" / run_id

        mountpoint.mkdir(parents=True, exist_ok=True)

        self._run([
            "zfs", "clone",
            "-p",  # auto-create parent datasets (e.g. <pool>/runs) — same fix as unpack()
            "-o", f"mountpoint={mountpoint}",
            snapshot_id,
            clone_dataset,
        ])

        handle = Handle(
            id=run_id,
            rootfs=mountpoint,
            dataset=clone_dataset,
            snapshot_id=snapshot_id,
        )

        log.info("clone(zfs): %s -> %s (rootfs=%s)", snapshot_id, clone_dataset, mountpoint)
        return mountpoint, handle
    # _clone_zfs:end

    # _clone_plaindir:start
    #   purpose: create a full directory copy of a plaindir snapshot for one run
    #   input:
    #     snapshot_id: str — absolute directory path produced by _unpack_plaindir()
    #                        or _register_base_plaindir()
    #   output:
    #     tuple[Path, Handle] — (dest Path, Handle with id/rootfs/dataset/snapshot_id)
    #   sideEffects: creates runs_dir via Path.mkdir;
    #                runs 'cp -a <src> <runs_dir>/<run_id>';
    #                removes <dest>/.jailrun_snap sentinel via Path.unlink if present
    def _clone_plaindir(self, snapshot_id: str) -> tuple[Path, Handle]:
        # snapshot_id is the directory path for plaindir
        src = Path(snapshot_id)
        # Check sentinel (either a dir with sentinel, or a base dir)
        if not src.exists():
            raise SnapshotNotFoundError(f"plaindir snapshot not found: {snapshot_id}")

        run_id = uuid.uuid4().hex
        runs_dir = Path(self._runs_ds)
        dest = runs_dir / run_id

        runs_dir.mkdir(parents=True, exist_ok=True)

        log.info("clone(plaindir): cp -a %s -> %s", src, dest)
        # Use cp -a to preserve permissions/symlinks; no CoW (degraded mode)
        self._run(["cp", "-a", str(src), str(dest)], timeout=DEFAULT_EXTRACT_TIMEOUT_S)

        # Remove the sentinel from the working copy (it's not part of the rootfs)
        sentinel = dest / ".jailrun_snap"
        if sentinel.exists():
            sentinel.unlink()

        handle = Handle(
            id=run_id,
            rootfs=dest,
            dataset=str(dest),
            snapshot_id=snapshot_id,
        )

        log.info("clone(plaindir): cloned to %s", dest)
        return dest, handle
    # _clone_plaindir:end

    # ------------------------------------------------------------------
    # Layer unpacking — umoci path (preferred)
    # ------------------------------------------------------------------

    # _unpack_umoci:start
    #   purpose: apply all OCI layers to rootfs using umoci (handles whiteouts spec-correctly)
    #   input:
    #     oci_dir: Path — local OCI layout directory
    #     tag: str — OCI image tag to unpack
    #     rootfs: Path — destination directory for the extracted rootfs
    #   output:
    #     none
    #   sideEffects: runs 'umoci raw unpack --image <oci_dir>:<tag> <rootfs>'
    #                (writes all layer content into rootfs, handles device nodes, xattrs,
    #                 hardlinks, and OCI whiteout markers)
    def _unpack_umoci(self, oci_dir: Path, tag: str, rootfs: Path) -> None:
        """
        Use umoci to apply all OCI layers to *rootfs*.

        Command::

            umoci raw unpack --image <oci_dir>:<tag> <rootfs>  # UNVERIFIED

        umoci handles:
        - Layering order (lowest → highest)
        - .wh.<name>  (delete target file/dir from lower layer)
        - .wh..wh..opq  (opaque whiteout — remove all siblings from lower layers)
        - Device nodes, hardlinks, symlinks, xattrs
        """
        img_spec = f"{oci_dir}:{tag}"
        log.info("_unpack_umoci: umoci raw unpack --image %s %s", img_spec, rootfs)
        self._run([                              # UNVERIFIED: umoci must be installed (sysutils/umoci)
            "umoci", "raw", "unpack",
            "--image", img_spec,
            str(rootfs),
        ], timeout=DEFAULT_EXTRACT_TIMEOUT_S)
    # _unpack_umoci:end

    # ------------------------------------------------------------------
    # Layer unpacking — bsdtar fallback path (manual whiteout handling)
    # ------------------------------------------------------------------

    # _unpack_bsdtar:start
    #   purpose: extract OCI layers into rootfs using bsdtar with manual OCI whiteout processing
    #   input:
    #     oci_dir: Path — local OCI layout directory
    #     tag: str — OCI image tag to unpack
    #     rootfs: Path — destination directory for the extracted rootfs
    #   output:
    #     none
    #   sideEffects: for each layer blob: creates a temp directory, runs 'bsdtar -xf <blob>
    #                -C <tmp> --no-same-owner'; applies opaque whiteouts by calling _rm_rf on
    #                siblings in rootfs; applies file whiteouts by calling _rm_rf on targets;
    #                copies non-whiteout files into rootfs via _merge_tree (shutil.copy2 + os.symlink);
    #                calls _remove_whiteout_markers to scrub residual .wh.* files from rootfs
    def _unpack_bsdtar(self, oci_dir: Path, tag: str, rootfs: Path) -> None:
        """
        Fallback: use bsdtar to extract OCI layers, then apply whiteouts manually.

        This is more fragile than umoci but avoids the umoci dependency.

        Algorithm (per OCI image-spec §layer):
        1. Read image index → find manifest for *tag*.
        2. Read manifest → collect layer blob digests in order (base first).
        3. For each layer blob:
           a. bsdtar -x into a temp dir.
           b. Apply opaque whiteouts (.wh..wh..opq) first:
              delete all existing siblings in rootfs under that directory.
           c. Apply file whiteouts (.wh.<name>): delete target in rootfs.
           d. Copy remaining files (not whiteout files) into rootfs.
        4. Remove any residual .wh.* markers from rootfs.

        Notes
        -----
        • Layer blobs are gzip-compressed tars stored under
          ``<oci_dir>/blobs/sha256/<digest>``.
        • bsdtar on FreeBSD handles hardlinks fine (confirmed 2026-07-20, real
          esphome/esphome:2025.5 image).
        • Device node creation requires root; whiteout markers are regular files
          in the OCI layer, which bsdtar can extract unprivileged except for devices.
        • xattrs are extracted with ``--no-xattrs`` (see below) -- Linux-only
          xattrs like ``security.capability`` have no FreeBSD equivalent and
          bsdtar-as-root treats a failure to restore them as fatal, aborting
          the whole unpack otherwise (confirmed 2026-07-20 on ping's
          cap_net_raw capability xattr).
        """
        # START_BSDTAR_MANIFEST_PARSE
        index_path = oci_dir / "index.json"
        if not index_path.exists():
            raise StoreError(f"OCI index not found: {index_path}")

        index = json.loads(index_path.read_text())
        manifest_desc = self._find_manifest_for_tag(index, tag, oci_dir)
        manifest_digest = manifest_desc["digest"]  # sha256:abc...
        manifest_blob = self._blob_path(oci_dir, manifest_digest)
        manifest = json.loads(manifest_blob.read_text())

        layers = manifest.get("layers", [])
        log.info("_unpack_bsdtar: applying %d layers into %s", len(layers), rootfs)
        # END_BSDTAR_MANIFEST_PARSE

        dst_real = rootfs.resolve()

        # START_BSDTAR_LAYER_LOOP
        for i, layer_desc in enumerate(layers):
            layer_digest = layer_desc["digest"]   # sha256:<hex>
            layer_blob = self._blob_path(oci_dir, layer_digest)
            log.debug("layer %d/%d: %s", i + 1, len(layers), layer_digest[:32])

            with tempfile.TemporaryDirectory(prefix="jailrun-layer-") as tmp:
                tmp_path = Path(tmp)

                # Extract layer into tmp
                #
                # --no-xattrs: many real-world OCI layers carry Linux-only
                # extended attributes (e.g. `security.capability` on
                # capability-tagged binaries like /usr/bin/ping, granting
                # cap_net_raw instead of setuid). bsdtar run as root (which
                # jailrun always is, for jail creation) attempts to restore
                # xattrs by default and treats a failure as fatal --
                # "Cannot restore extended attributes: security.capability:
                # Unknown error: -1" -- aborting the ENTIRE unpack even though
                # the file content itself extracted fine. Confirmed live
                # 2026-07-20 against esphome/esphome:2025.5's ping binary.
                # FreeBSD has no equivalent capability model, so there's
                # nothing meaningful to restore anyway -- safe to skip
                # unconditionally rather than only as a root-vs-non-root
                # default.
                self._run([
                    "bsdtar", "-xf", str(layer_blob),
                    "-C", str(tmp_path),
                    "--no-same-owner",
                    "--no-xattrs",
                ], timeout=DEFAULT_EXTRACT_TIMEOUT_S)

                # START_BSDTAR_OPAQUE_WHITEOUTS
                # Apply opaque whiteouts first
                for wh_opq in sorted(tmp_path.rglob(".wh..wh..opq")):
                    container_dir = rootfs / wh_opq.parent.relative_to(tmp_path)
                    if container_dir.exists():
                        log.debug("opaque whiteout: clearing %s", container_dir)
                        _clear_opaque_whiteout(container_dir, dst_real)
                # END_BSDTAR_OPAQUE_WHITEOUTS

                # START_BSDTAR_FILE_WHITEOUTS
                # Apply file whiteouts
                for wh_file in sorted(tmp_path.rglob(".wh.*")):
                    if wh_file.name == ".wh..wh..opq":
                        continue
                    target_name = wh_file.name[len(".wh."):]
                    container_target = rootfs / wh_file.parent.relative_to(tmp_path) / target_name
                    if container_target.exists() or container_target.is_symlink():
                        log.debug("whiteout: removing %s", container_target)
                        _apply_file_whiteout(container_target, dst_real)
                # END_BSDTAR_FILE_WHITEOUTS

                # Copy non-whiteout files into rootfs
                _merge_tree(tmp_path, rootfs)
        # END_BSDTAR_LAYER_LOOP

        # Scrub any residual whiteout markers from rootfs
        _remove_whiteout_markers(rootfs)
        log.info("_unpack_bsdtar: layers applied")
    # _unpack_bsdtar:end

    # ------------------------------------------------------------------
    # OCI / content-addressing helpers
    # ------------------------------------------------------------------

    # _oci_dir_for: maps image_ref to a sanitised cache subdirectory path (pure, no IO)
    def _oci_dir_for(self, image_ref: str) -> Path:
        """Return the OCI cache directory path for an image reference."""
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", image_ref)
        return self.oci_cache_dir / safe

    # _find_oci_for_image_id:start
    #   purpose: locate the OCI layout directory in the local cache that matches image_id
    #   input:
    #     image_id: str — sha256 hex produced by _compute_image_id / resolve()
    #   output:
    #     tuple[Path, str] — (oci_dir, tag) where tag is always "latest"
    #   sideEffects: reads oci_cache_dir via Path.iterdir(); reads index.json and manifest
    #                blobs inside each candidate directory via json.loads / Path.read_text
    def _find_oci_for_image_id(self, image_id: str) -> tuple[Path, str]:
        """
        Walk the OCI cache to find the oci_dir that produced *image_id*.

        We re-compute the image_id from each cached OCI layout until we find
        a match.  Raises ImageNotFoundError if not found.

        Returns
        -------
        tuple[Path, str]
            (oci_dir, tag) where tag is always "latest" in the current scheme.
        """
        tag = "latest"
        if not self.oci_cache_dir.exists():
            raise ImageNotFoundError(f"OCI cache is empty; run resolve() first")

        for candidate in self.oci_cache_dir.iterdir():
            if not candidate.is_dir():
                continue
            try:
                cid = self._compute_image_id(candidate, tag)
            except Exception:
                continue
            if cid == image_id:
                return candidate, tag

        raise ImageNotFoundError(
            f"image_id {image_id!r} not found in OCI cache {self.oci_cache_dir}"
        )
    # _find_oci_for_image_id:end

    # _compute_image_id:start
    #   purpose: derive a stable content-addressed image_id from an OCI layout on disk
    #   input:
    #     oci_dir: Path — local OCI layout directory containing index.json and blobs/
    #     tag: str — OCI image tag whose manifest to read
    #   output:
    #     image_id: str — sha256 hex of sorted layer digest strings joined with newline
    #   sideEffects: reads <oci_dir>/index.json and the manifest blob file via Path.read_text
    def _compute_image_id(self, oci_dir: Path, tag: str) -> str:
        """
        Compute a content-addressed image_id from an OCI layout.

        Algorithm: sha256(sorted_layer_digests_joined_with_newline)

        The sorted order makes the ID stable regardless of the order in which
        skopeo wrote blobs.  The OCI spec already requires a deterministic layer
        order in the manifest, so the sorted() here primarily guards against
        any ordering variation in how we read the manifest.

        In practice, image_id = sha256(layer[0]_digest + "\\n" + layer[1]_digest + …)
        where digests are the sha256:hex strings from the manifest's layers array
        (sorted lexicographically so the ID is reproducible).
        """
        index_path = oci_dir / "index.json"
        if not index_path.exists():
            raise StoreError(f"No OCI index at {index_path}")

        index = json.loads(index_path.read_text())
        manifest_desc = self._find_manifest_for_tag(index, tag, oci_dir)
        manifest_blob = self._blob_path(oci_dir, manifest_desc["digest"])
        manifest = json.loads(manifest_blob.read_text())

        layer_digests = sorted(
            layer["digest"] for layer in manifest.get("layers", [])
        )
        key = "\n".join(layer_digests)
        return hashlib.sha256(key.encode()).hexdigest()
    # _compute_image_id:end

    # CONTRACT: read index.get("manifests") -> match by OCI ref.name annotation
    #           -> fallback to single-manifest index -> raise StoreError if ambiguous
    def _find_manifest_for_tag(
        self, index: dict, tag: str, oci_dir: Path
    ) -> dict:
        """
        Find the manifest descriptor for *tag* inside an OCI image index.

        OCI index.json → manifests[] → find entry where
        annotations["org.opencontainers.image.ref.name"] == tag.
        If there is exactly one manifest and no annotations, returns it directly.
        """
        manifests = index.get("manifests", [])
        # Try annotation match first
        for m in manifests:
            ann = m.get("annotations", {})
            if ann.get("org.opencontainers.image.ref.name") == tag:
                return m
        # Single-manifest index — return unconditionally
        if len(manifests) == 1:
            return manifests[0]
        raise StoreError(
            f"Cannot find manifest for tag {tag!r} in index with "
            f"{len(manifests)} entries"
        )

    # _blob_path: splits 'sha256:<hex>' digest -> returns <oci_dir>/blobs/sha256/<hex> Path (pure, no IO)
    @staticmethod
    def _blob_path(oci_dir: Path, digest: str) -> Path:
        """
        Resolve a digest string (``sha256:<hex>``) to its blob file path.

        OCI layout stores blobs at  <oci_dir>/blobs/<alg>/<hex>.
        """
        alg, hex_digest = digest.split(":", 1)
        return oci_dir / "blobs" / alg / hex_digest

    # ------------------------------------------------------------------
    # ZFS helpers
    # ------------------------------------------------------------------

    # _zfs_snapshot_exists:start
    #   purpose: probe whether a ZFS snapshot name exists on the current host
    #   input:
    #     snapshot_id: str — full ZFS snapshot name e.g. 'jailrun/images/abc@snap'
    #   output:
    #     exists: bool — True if 'zfs list' exits 0 for that snapshot name
    #   sideEffects: runs 'zfs list -H -t snapshot -o name <snapshot_id>'
    def _zfs_snapshot_exists(self, snapshot_id: str) -> bool:
        """Return True if a ZFS snapshot exists.  # UNVERIFIED"""
        rc, _out, _err = self._run_ok([
            "zfs", "list", "-H", "-t", "snapshot", "-o", "name", snapshot_id,
        ])
        return rc == 0
    # _zfs_snapshot_exists:end

    # ------------------------------------------------------------------
    # subprocess helpers
    # ------------------------------------------------------------------

    # _run:start
    #   purpose: run an external command, raise StoreError on non-zero exit or timeout
    #   input:
    #     cmd: list[str] — command and arguments
    #     cwd: str | None — working directory for the subprocess
    #     input_: bytes | None — optional stdin bytes
    #     timeout: float | None — seconds before killing the process; None = no limit
    #              (default DEFAULT_LOCAL_TIMEOUT_S — see call sites for network/
    #              provision overrides; a hung fetch or wedged
    #              build must be killable, never left to hang forever)
    #   output:
    #     result: subprocess.CompletedProcess
    #   sideEffects: spawns subprocess via subprocess.run (capture_output=True);
    #                logs command at DEBUG; logs stderr at ERROR on failure;
    #                raises StoreError if returncode != 0, on timeout, or if the
    #                binary itself cannot be spawned (OSError)
    def _run(
        self,
        cmd: list[str],
        cwd: str | None = None,
        input_: bytes | None = None,
        timeout: float | None = DEFAULT_LOCAL_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """
        Run *cmd*, raise StoreError on non-zero exit, timeout, or spawn failure.
        Logs the command at DEBUG level and stderr at ERROR on failure.

        env, if given, is merged over a copy of the current process environment
        (not a replacement) — used by register_base() to hand the base's own
        mountpoint to provision_cmd via JAILRUN_BASE_ROOT (see plan_to_provision_cmd).
        """
        log.debug("run: %s", shlex.join(cmd))
        full_env = {**os.environ, **env} if env else None
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                input=input_,
                capture_output=True,
                timeout=timeout,
                env=full_env,
            )
        except subprocess.TimeoutExpired as exc:
            log.error("command timed out after %ss: %s", timeout, shlex.join(cmd))
            raise StoreError(
                f"Command timed out after {timeout}s: {shlex.join(cmd)}"
            ) from exc
        except OSError as exc:
            log.error("command failed to start: %s: %s", shlex.join(cmd), exc)
            raise StoreError(f"Command failed to start: {shlex.join(cmd)}: {exc}") from exc
        if result.returncode != 0:
            log.error(
                "command failed (rc=%d): %s\nstderr: %s",
                result.returncode,
                shlex.join(cmd),
                result.stderr.decode(errors="replace"),
            )
            raise StoreError(
                f"Command failed (rc={result.returncode}): {shlex.join(cmd)}"
            )
        return result
    # _run:end

    # _run_ok:start
    #   purpose: run an external command without raising on failure; return exit status
    #   input:
    #     cmd: list[str] — command and arguments
    #     cwd: str | None — working directory for the subprocess
    #     timeout: float | None — seconds before killing the process; None = no limit
    #   output:
    #     tuple[int, bytes, bytes] — (returncode, stdout, stderr); rc=-1 with an
    #             error message in stderr on timeout or spawn failure (never raises)
    #   sideEffects: spawns subprocess via subprocess.run (capture_output=True)
    def _run_ok(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: float | None = DEFAULT_LOCAL_TIMEOUT_S,
    ) -> tuple[int, bytes, bytes]:
        """Run *cmd*; return (returncode, stdout, stderr) without raising."""
        log.debug("run_ok: %s", shlex.join(cmd))
        try:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("command timed out after %ss (tolerated): %s", timeout, shlex.join(cmd))
            return -1, b"", f"timed out after {timeout}s".encode()
        except OSError as exc:
            log.warning("command failed to start (tolerated): %s: %s", shlex.join(cmd), exc)
            return -1, b"", str(exc).encode()
        return result.returncode, result.stdout, result.stderr
    # _run_ok:end
# Store:end


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

# _seed_pkg_trust_keys:start
#   purpose: copy the host's pkg(8) trusted-signature-key directories into a
#            freshly created (empty) base mountpoint, before running provision_cmd
#   input:
#     mountpoint: Path — the empty base dataset/directory about to be provisioned
#   output: none
#   sideEffects: creates <mountpoint>/usr/share/keys/ and copies
#                /usr/share/keys/pkg and /usr/share/keys/pkgbase-* into it
#                (best-effort — missing source dirs are silently skipped)
#   rationale: `pkg -r <rootdir>` is "not a chroot" (per pkg(8)) but DOES look for
#              trusted-signature keys under <rootdir>/usr/share/keys/ rather than
#              the host's own copy. A freshly `zfs create`d dataset has none, so
#              every repo's catalog fetch silently fails signature verification —
#              confirmed live 2026-07-19: pkg reported "All
#              repositories are up to date" despite "Error opening the trusted
#              directory" warnings, then the very next `pkg install` failed with
#              "Repository ... cannot be opened. 'pkg update' required" because
#              the catalog was never actually trusted/saved. A real base.txz-seeded
#              root would already have these; this dataset isn't one, so seed them
#              explicitly. The keys are FreeBSD's own public trust anchors (not
#              secret), so copying them is safe.
def _seed_pkg_trust_keys(mountpoint: Path) -> None:
    src_keys = Path("/usr/share/keys")
    if not src_keys.is_dir():
        return
    dst_keys = mountpoint / "usr" / "share" / "keys"
    dst_keys.mkdir(parents=True, exist_ok=True)
    for candidate in src_keys.iterdir():
        if candidate.name != "pkg" and not candidate.name.startswith("pkgbase-"):
            continue
        dst = dst_keys / candidate.name
        if dst.exists():
            continue
        try:
            shutil.copytree(candidate, dst, symlinks=True)
        except OSError as exc:
            log.warning("failed to seed pkg trust keys %s -> %s: %s", candidate, dst, exc)
# _seed_pkg_trust_keys:end


# _sha256_str: returns sha256 hex digest of UTF-8-encoded string s (pure, no IO)
def _sha256_str(s: str) -> str:
    """SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(s.encode()).hexdigest()


# _safe_zfs_name: replaces chars outside [a-zA-Z0-9._-] with '_' for ZFS dataset name safety (pure, no IO)
def _safe_zfs_name(s: str) -> str:
    """
    Sanitise a string for use as a ZFS dataset name component.

    ZFS allows alphanumeric, '-', '_', '.', ':'.  Colons are legal in dataset
    names but can confuse shell scripts; we replace everything non-alphanumeric
    except '-' and '_' with '_'.
    """
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


# _rm_rf:start
#   purpose: remove a filesystem path regardless of type (file, symlink, or directory tree)
#   input:
#     path: Path — path to remove
#   output:
#     none
#   sideEffects: calls path.unlink() for files/symlinks; calls shutil.rmtree for directories
def _rm_rf(path: Path) -> None:
    """Remove a file, symlink, or directory tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(str(path))
# _rm_rf:end


# _within:start
#   purpose: security guard — test whether a filesystem path, fully resolved (following any
#            symlinks in its existing prefix), stays inside root (root itself or a descendant)
#   input:
#     path: Path — candidate path (may not exist yet)
#     root: Path — already-resolved containment root
#   output:
#     bool — True iff path resolves to root or under it; False on escape or resolution error
#   sideEffects: none (read-only Path.resolve)
def _within(path: Path, root: Path) -> bool:
    try:
        rp = path.resolve()
    except (OSError, RuntimeError):
        # symlink loop / bad path — treat as an escape (fail closed)
        return False
    return rp == root or root in rp.parents
# _within:end


# _merge_tree:start
#   purpose: recursively merge src directory tree into dst, skipping OCI whiteout marker files,
#            REFUSING any entry that would write outside dst (malicious-layer path-escape guard)
#   input:
#     src: Path — source directory (typically an extracted layer tmp dir)
#     dst: Path — destination rootfs directory
#   output:
#     none
#   sideEffects: for each non-whiteout item in src: creates target directory via Path.mkdir;
#                recreates symlinks via os.symlink (removes existing target first with Path.unlink);
#                copies regular files via shutil.copy2 (preserves timestamps).
#                Raises StoreError (fail-closed) if an entry's resolved parent escapes dst.
def _merge_tree(src: Path, dst: Path) -> None:
    """
    Recursively copy *src* into *dst*, skipping whiteout marker files.

    Uses shutil.copy2 for regular files (preserves timestamps); shutil.copytree
    would overwrite the destination which is not what we want (we are merging
    successive layers).

    SECURITY: a malicious OCI layer can ship a symlink (e.g. ``usr/x -> /``) so that a
    later entry (``usr/x/authorized_keys``) is written *through* it, escaping the rootfs
    onto the host (this runs as root). We resolve each target's parent — following any
    symlinks created by earlier entries or layers — and refuse anything that lands outside
    ``dst``, and we never descend into / write through an inherited symlink.
    """
    dst_real = dst.resolve()
    for item in src.rglob("*"):
        if item.name.startswith(".wh."):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        # Fail closed if the resolved parent escapes the rootfs (symlink write-through).
        if not _within(target.parent, dst_real):
            raise StoreError(
                f"OCI layer entry {str(rel)!r} escapes rootfs "
                f"(resolves outside {dst_real}); refusing to unpack"
            )
        if item.is_symlink():
            link_target = os.readlink(item)
            if target.is_symlink() or target.exists():
                target.unlink()
            os.symlink(link_target, target)
        elif item.is_dir():
            # Never treat an inherited symlink as a directory to descend into.
            if target.is_symlink():
                target.unlink()
            target.mkdir(exist_ok=True)
        else:
            # Never write through an inherited symlink; replace it with the real file.
            if target.is_symlink():
                target.unlink()
            shutil.copy2(str(item), str(target))
# _merge_tree:end


# _clear_opaque_whiteout:start
#   purpose: delete all children of an opaque-whiteout target directory, fail-closed
#            if it escapes the rootfs (second symlink-escape variant,
#            NOT covered by the original _merge_tree fix)
#   input:
#     container_dir: Path — the directory inside rootfs whose children should be cleared
#     dst_real: Path — resolved rootfs root (containment boundary)
#   output: none
#   sideEffects: deletes every child of container_dir via _rm_rf; raises StoreError
#                without deleting anything if container_dir itself resolves outside dst_real
#   rationale: an EARLIER layer's _merge_tree call may legitimately have planted a
#              symlink at this exact path (e.g. usr/evil -> /etc) — that symlink's
#              mere existence is allowed (real images do this), but THIS layer's
#              opaque-whiteout marker under that path must not cause us to
#              iterate/delete through it onto the host. container_dir.exists() and
#              .iterdir() both follow symlinks, so the check must happen first.
def _clear_opaque_whiteout(container_dir: Path, dst_real: Path) -> None:
    if not _within(container_dir, dst_real):
        raise StoreError(
            f"opaque whiteout target {container_dir} resolves outside {dst_real} "
            "(symlink escape via an earlier layer); refusing to unpack"
        )
    for child in list(container_dir.iterdir()):
        _rm_rf(child)
# _clear_opaque_whiteout:end


# _apply_file_whiteout:start
#   purpose: delete a file-whiteout target, fail-closed if its parent escapes the rootfs
#   input:
#     container_target: Path — the file/symlink inside rootfs to remove
#     dst_real: Path — resolved rootfs root (containment boundary)
#   output: none
#   sideEffects: removes container_target via _rm_rf; raises StoreError without
#                deleting anything if container_target's parent resolves outside dst_real
#   rationale: same escape class as _clear_opaque_whiteout — checking the PARENT
#              (not container_target itself) matches _merge_tree's existing pattern:
#              _rm_rf on a symlink just unlinks the link itself (never follows it),
#              so the only way to reach outside dst_real is via a symlinked ancestor
#              directory earlier in the path
def _apply_file_whiteout(container_target: Path, dst_real: Path) -> None:
    if not _within(container_target.parent, dst_real):
        raise StoreError(
            f"file whiteout target {container_target} resolves outside {dst_real} "
            "(symlink escape via an earlier layer); refusing to unpack"
        )
    _rm_rf(container_target)
# _apply_file_whiteout:end


# _remove_whiteout_markers:start
#   purpose: scrub any residual .wh.* marker files from the fully assembled rootfs
#   input:
#     rootfs: Path — assembled rootfs directory to clean
#   output:
#     none
#   sideEffects: calls Path.unlink on each .wh.* file found via rootfs.rglob (OSError ignored)
def _remove_whiteout_markers(rootfs: Path) -> None:
    """Delete any residual .wh.* files from the assembled rootfs."""
    for wh in rootfs.rglob(".wh.*"):
        try:
            wh.unlink()
        except OSError:
            pass
# _remove_whiteout_markers:end

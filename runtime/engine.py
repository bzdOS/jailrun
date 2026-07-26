# START_AI_HEADER
# MODULE: runtime/engine.py
# PURPOSE: S1 runtime core — hybrid native-first jail orchestrator
# INTENT: Coordinates S3/S2/S4 seams to unpack an image into a ZFS clone, build a
#         native-first PATH shadow layer, optionally load Linuxulator, start a
#         FreeBSD jail via jail(8), run the user command with jexec(8), stream I/O
#         (teeing it to a persisted per-run log file for `jailrun logs`),
#         and tear down the jail and optionally destroy the clone on exit.
# DEPENDENCIES: stdlib (asyncio, contextlib, json, logging, os, re, shlex,
#               signal, subprocess, tempfile, textwrap, pathlib);
#               store (S3 seam — resolve/unpack/clone/mount/destroy ZFS datasets);
#               probe (S2 seam — produces substitution manifest for a rootfs);
#               bakery (S4 seam — fills native.artifact_path in the manifest);
#               runtime._mocks (fallback stubs when real seams are absent);
#               runtime.lifecycle (native jexec teardown, imported lazily);
#               runtime.rundb (RunDB run-state ledger, imported lazily — see the
#               record_start/record_exit call sites in _run_async, and the
#               best-effort liveness check in _check_jail_live for exec_run);
#               FreeBSD system tools: jail(8), jexec(8), jls(8), kldload(8), nullfs(5)
# PUBLIC_API: run(image_ref, cmd, opts) -> int;
#             exec_run(jail_name, cmd, opts) -> int (`jailrun exec` — runs one
#             more command against an ALREADY-RUNNING jail; does not create or
#             destroy anything, unlike run()/_run_async())
# END_AI_HEADER

# START_INVARIANTS
# - The ZFS clone (rootfs_path) is always created before any mutation of its tree.
# - _assemble_native_shadow only writes into <rootfs>/jailrun-native/bin/ — never
#   into the image's own /usr/bin or /usr/local/bin.
# - The jail is started in persist mode; it is always removed in the finally block
#   regardless of whether jexec succeeded.
# - kldload linux64 is called with check=False (idempotent; already-loaded is fine).
# - RunDB.record_start()/record_exit() calls are wrapped in their own try/except
#   (defense in depth on top of RunDB's own internal error handling): a broken
#   run-state ledger must NEVER prevent a jail from starting, running, or
#   tearing down (see runtime/rundb.py's module NOTE — this is that deferred wiring).
# END_INVARIANTS

# START_RATIONALE
# Q: Why jexec and not jail exec.start?
# A: jail exec.start returns rc=0 on successful dispatch, losing the command's own
#    exit code. jexec propagates the inner process returncode faithfully — critical
#    for container-style usage where callers inspect the exit code.
# Q: Why symlinks in /jailrun-native/bin rather than bind-mounts for the shadow layer?
# A: Symlinks exist inside the ZFS clone itself, so no extra mount parameters are
#    needed in jail.conf for the shadow layer proper. They are CoW-cheap. The bakery
#    base ITSELF (which actually owns the native binaries) lives in its own separate
#    dataset and IS bind-mounted in (at NATIVE_BASE_MOUNT) — a prior version of
#    this comment wrongly assumed the clone "inherited" the base.
# Q: Why mount += rather than mount.fstab in jail.conf?
# A: mount.fstab replaces the entire fstab and silently discards subsequent mounts.
#    mount += is additive and safe for multiple -v bind volumes.
# END_RATIONALE
"""
jailrun engine — S1 runtime core.

Orchestrates S3 (store), S2 (probe), S4 (bakery) to:
  1. Resolve + unpack image into a ZFS snapshot.
  2. Clone into a CoW writable rootfs.
  3. Load the substitution manifest.
  4. Assemble the jail: native-first PATH shadowing + -v bind mounts.
  5. Conditionally enable Linux ABI (Linuxulator) only when needed.
  6. jail -c + jexec the command; stream stdout/stderr; return exact exit code.
  7. On --rm: destroy the clone.

DESIGN PRINCIPLES (baked in from prior research):
  [GOTCHA] Use `jexec` not `exec.start` for exit codes — jail exec.start returns
           0 on successful dispatch regardless of the command exit code; jexec
           propagates the inner process returncode.
  [GOTCHA] Jail parameters: use `mount +=` (additive) not `mount.fstab` — the
           latter replaces the fstab entirely and silently breaks subsequent mounts.
  [GOTCHA] fdescfs must be mounted with `-o linrdlnk` when Linuxulator is active so
           that /proc/self/fd symlinks resolve correctly under the Linux ABI.
  [GOTCHA] nullfs has no uid/gid remapping — bind-mounted host paths appear with
           host uid numbers inside the jail. Document; do not silently ignore.

MOCKED SEAMS (other agents own these; we import against the published API contract):
  store  — S3; see ARCHITECTURE.md Seam 2 for the Store API.
  probe  — S2; emits a substitution manifest for an unpacked rootfs.
  bakery — S4; fills native.artifact_path in the manifest.

py_compile-clean: the mock imports below resolve at import time even on Linux.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("jailrun.engine")

# Per-run jail IP tracking for 0.4 destination-scoped network — ensures
# concurrent _run_async calls don't collide on loopback aliases. Each call
# allocates from alloc_jail_ip() (default 10.99.0.0/24 pool), adds to
# _netpolicy_used_ips, and removes on teardown.
_netpolicy_used_ips: set[str] = set()
_netpolicy_lock: Any = None
try:
    import threading
    _netpolicy_lock = threading.Lock()
except ImportError:
    pass


# ===========================================================================
# Mock seam imports — replaced by real implementations when S2/S3/S4 land.
# ===========================================================================

try:
    # store.store.Store's public API (resolve/unpack/clone/mount/destroy/...) is
    # defined on the Store CLASS, not as module-level functions — unlike probe.py's
    # probe() and bakery.py's bake(), which genuinely are module-level. Importing
    # the bare module and calling `_store_module.resolve(...)` on it (the previous
    # code) raised AttributeError the first time this ever ran for real (2026-07-19)
    # — it only "worked" against the mock because MockStore uses
    # @staticmethod, which masked the mismatch. Instantiate the real class instead.
    from store.store import Store as _Store  # type: ignore[import-not-found]
    _store_module = _Store()
except ImportError:  # running on Linux / seam not yet built
    from runtime._mocks import MockStore as _store_module  # type: ignore[assignment]

try:
    from probe import probe as _probe_module  # type: ignore[import-not-found]
except ImportError:
    from runtime._mocks import MockProbe as _probe_module  # type: ignore[assignment]

try:
    from bakery import bakery as _bakery_module  # type: ignore[import-not-found]
except ImportError:
    from runtime._mocks import MockBakery as _bakery_module  # type: ignore[assignment]


# ===========================================================================
# Manifest helpers
# ===========================================================================

MANIFEST_FILENAME = "substitution-manifest.json"

# Persistent cache for pre-computed substitution manifests, keyed by image_id.
# Avoids re-running probe+bakery when the same image is run again.
# Falls back to <rootfs>/.jailrun/<MANIFEST_FILENAME> (per-clone cache) on miss.
MANIFEST_CACHE_DIR = "/var/cache/jailrun/manifests"

# Where the native shadow binaries land inside the rootfs clone.
NATIVE_BIN_DIR = "/jailrun-native/bin"


# _is_jailed:start
#   purpose: detect whether jailrun ITSELF is running inside a FreeBSD jail
#            (nested-jail deployment, e.g. jailrun running inside a host
#            application's production jail). Governs the devfs ruleset choice
#            in _build_jail_conf below.
#   output: bool — True if security.jail.jailed reports a nonzero value.
def _is_jailed() -> bool:
    """Return True when this process is itself running inside a jail."""
    try:
        import subprocess  # noqa: PLC0415
        out = subprocess.run(
            ["sysctl", "-n", "security.jail.jailed"],
            capture_output=True, text=True, timeout=5,
        )
        return out.returncode == 0 and out.stdout.strip() not in ("", "0")
    except Exception:
        # Fail closed toward the bare-host behavior (ruleset 4). If we truly
        # were jailed and misdetected, jail creation fails loudly with EPERM
        # rather than silently mounting an unrestricted devfs.
        return False


# _devfs_delegation_available:start
#   purpose: empirically test whether the jail jailrun itself is running
#            inside (when nested) has been configured to delegate devfs
#            ruleset 4 to its own children -- i.e. whether it was created
#            with allow.mount / allow.mount.devfs / enforce_statfs=1 /
#            children.max>=1. Only meaningful when _is_jailed() is True;
#            callers must check that first.
#   output: bool -- True if a real throwaway probe jail with a bare
#           `mount.devfs;` (no explicit ruleset requested) was created
#           successfully, meaning nested run-jails get the safe, restricted
#           ruleset 4 with zero opt-in needed. False if the probe failed
#           (jail(8) returns EPERM the moment the parent lacks the allow.*
#           bits above) or the environment can't be tested at all
#           (non-FreeBSD dev host, jail(8) missing).
#   sideEffects: creates and immediately destroys one throwaway jail plus a
#                scratch directory under tempfile.mkdtemp(); no caller-visible
#                state survives this call either way.
def _devfs_delegation_available() -> bool:
    """
    Probe whether nested run-jails can get devfs ruleset 4 natively.

    Empirically confirmed (2026-07-26): a nested jail's own `mount.devfs;`
    succeeds and correctly gets ruleset 4 -- with NO host-side daemon and NO
    privileged helper process -- as long as the jail jailrun is itself
    running inside was created with `allow.mount allow.mount.devfs
    enforce_statfs=1 devfs_ruleset=4 children.max>=1`. If it wasn't (true for
    most nested deployments today), the identical `mount.devfs;` fails
    outright with "Operation not permitted" -- there is no silent
    unrestricted fallback to guard against at the kernel level; `jail -c`
    simply refuses, which is exactly what this probe detects. This
    supersedes an earlier assumption (see CHANGELOG) that nested devfs
    delegation was categorically kernel-blocked and therefore needed a
    privileged host-side daemon to work around -- it doesn't; the fix is a
    configuration requirement on the OUTER jail, not new jailrun-side
    privilege.
    """
    probe_name = f"jailrun-devfs-probe-{os.getpid()}"
    scratch = tempfile.mkdtemp(prefix="jailrun-devfs-probe-")
    created = False
    try:
        os.makedirs(os.path.join(scratch, "dev"), exist_ok=True)
        r = subprocess.run(
            [
                "jail", "-c",
                f"path={scratch}",
                f"name={probe_name}",
                "persist", "ip4=disable", "ip6=disable", "mount.devfs",
            ],
            capture_output=True, timeout=10,
        )
        created = r.returncode == 0
        return created
    except Exception:
        return False
    finally:
        if created:
            teardown = subprocess.run(
                ["jail", "-r", probe_name],
                capture_output=True, timeout=10,
            )
            if teardown.returncode != 0:
                log.warning(
                    "devfs-delegation probe jail %s failed to tear down: %s",
                    probe_name, teardown.stderr.decode(errors="replace").strip(),
                )
        shutil.rmtree(scratch, ignore_errors=True)
# _devfs_delegation_available:end


# _load_manifest:start
#   purpose: resolve the substitution manifest for image_ref, using the cached
#            file inside the rootfs clone or running probe+bakery on a cache miss
#   input:
#     rootfs_path: str — absolute path to the writable ZFS clone root
#     image_ref: str — image reference (used for logging and passed to probe)
#     image_id: str | None — resolved image content hash; if provided, enables
#               a persistent store-level cache at <MANIFEST_CACHE_DIR>/<image_id>.json
#   output:
#     manifest: dict[str, Any] — parsed substitution manifest
#   sideEffects: on cache miss — calls _probe_module.probe(rootfs_path, image_ref)
#                and _bakery_module.bake(manifest); creates directory
#                <rootfs_path>/.jailrun/ and writes <rootfs_path>/.jailrun/substitution-manifest.json
#                and, if image_id is given, writes a persistent copy to
#                <MANIFEST_CACHE_DIR>/<image_id>.json
def _load_manifest(rootfs_path: str, image_ref: str, image_id: str | None = None) -> dict[str, Any]:
    """

    Load the substitution manifest for *image_ref*.

    Checks, in order:
      1. image-level persistent cache at <MANIFEST_CACHE_DIR>/<image_id>.json
      2. per-clone cache at   <rootfs_path>/.jailrun/<MANIFEST_FILENAME>
      3. probe + bakery (slow path)

    Falls back to calling probe + bakery inline if the manifest is absent
    (first-run / no pre-bake).  This is the slow path.
    """
    # START_CHECK_PERSISTENT_CACHE
    if image_id:
        persistent = Path(MANIFEST_CACHE_DIR) / f"{image_id}.json"
        if persistent.exists():
            log.debug("manifest cache hit (image_id=%s)", image_id)
            with persistent.open() as fh:
                return json.load(fh)
    # END_CHECK_PERSISTENT_CACHE

    # START_CHECK_CLONE_CACHE
    candidate = Path(rootfs_path) / ".jailrun" / MANIFEST_FILENAME
    if candidate.exists():
        log.debug("manifest found at %s", candidate)
        with candidate.open() as fh:
            return json.load(fh)
    # END_CHECK_CLONE_CACHE

    # START_PROBE_BAKE_SLOW_PATH
    log.info(
        "manifest not found for %s; running probe + bakery (slow path)", image_ref
    )
    manifest = _probe_module.probe(rootfs_path, image_ref)
    manifest = _bakery_module.bake(manifest)

    # Write per-clone cache.
    candidate.parent.mkdir(parents=True, exist_ok=True)
    with candidate.open("w") as fh:
        json.dump(manifest, fh, indent=2)

    # Write persistent cache keyed by image_id (skips probe+bakery on re-run).
    if image_id:
        persistent = Path(MANIFEST_CACHE_DIR) / f"{image_id}.json"
        persistent.parent.mkdir(parents=True, exist_ok=True)
        with persistent.open("w") as fh:
            json.dump(manifest, fh, indent=2)
        log.debug("persistent manifest cache written for image_id=%s", image_id)

    return manifest
    # END_PROBE_BAKE_SLOW_PATH
# _load_manifest:end


# ===========================================================================
# Native-first PATH shadowing
# ===========================================================================

# Where the bakery-registered base is bind-mounted inside the run's clone —
# native.artifact_path values are absolute paths on the BASE's own mountpoint
# (e.g. "/usr/local/bin/python3.11"), so the in-jail equivalent is this prefix
# joined with that path (previously nothing mounted the
# base into the clone at all — shadow symlinks pointed at a host path invisible
# from inside the jail's own chroot).
NATIVE_BASE_MOUNT = "/jailrun-native/base"


# _assemble_native_shadow:start
#   purpose: populate <rootfs>/jailrun-native/bin/ with symlinks so native
#            FreeBSD binaries shadow any Linux equivalents when PATH is prepended
#   input:
#     rootfs_path: str — absolute path to the writable ZFS clone root
#     manifest: dict[str, Any] — substitution manifest; only entries where
#               status == "native" and native.artifact_path is set are processed
#     base_prefix: str | None — where the bakery base is bind-mounted inside the
#               jail (NATIVE_BASE_MOUNT), or None if no base was registered/mounted
#               for this run (e.g. the image needed no native substitutes at all)
#   output:
#     none: None — mutates the rootfs filesystem
#   sideEffects: creates directory <rootfs_path>/jailrun-native/bin/ (mkdir -p);
#                for each qualifying manifest entry creates a symlink inside that
#                directory pointing to base_prefix + artifact_path
#   rationale: symlinks inside the clone avoid extra jail.conf mount parameters
#              and are CoW-cheap; enforces ARCHITECTURE.md's artifact-reality
#              invariant (status:"native" must mean a real artifact WILL exist at
#              that path) by checking existence, through the actual mount, before
#              ever creating a symlink — never shadow a phantom binary
def _assemble_native_shadow(
    rootfs_path: str,
    manifest: dict[str, Any],
    base_prefix: str | None = None,
) -> None:
    """
    Build the native-first shadow layer at <rootfs_path>/jailrun-native/bin/.

    Strategy:
      For every binary whose status == "native" and native.artifact_path is set:
        - artifact_path (e.g. /usr/local/bin/python3.11) is an absolute path on
          the bakery-registered BASE's own mountpoint, not inside any particular
          image clone. The caller (_run_async) bind-mounts that base into THIS
          clone at NATIVE_BASE_MOUNT before calling this function, so the
          in-jail equivalent is base_prefix + artifact_path.
        - We create a symlink at <rootfs_path>/jailrun-native/bin/<basename>
          pointing to that in-jail path, so that prepending /jailrun-native/bin
          to PATH inside the jail causes lookups to find the native binary
          first, shadowing any Linux binary at /usr/bin/<basename>.
        - Before creating the symlink we verify the target actually exists
          (through the mount, on the host, since the mount already happened) —
          the artifact-reality invariant: never shadow a phantom binary.

    Why symlinks rather than bind-mounts (for the shadow layer itself):
      - We need the shadow layer to exist INSIDE the rootfs so `jail -c` sees it
        without extra mount parameters.
      - Symlinks are CoW-cheap — the ZFS clone bears the cost, not the base.

    Why /jailrun-native/bin:
      - Avoids colliding with the image's /usr/local/bin or /usr/bin.
      - Easy to prepend: PATH=/jailrun-native/bin:$PATH.
      - Namespaced to jailrun so operators can audit / remove it cleanly.
    """
    # START_CREATE_SHADOW_DIR
    shadow_dir = Path(rootfs_path) / "jailrun-native" / "bin"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    log.debug("native shadow dir: %s", shadow_dir)
    # END_CREATE_SHADOW_DIR

    # START_POPULATE_SHADOW_SYMLINKS
    for entry in manifest.get("binaries", []):
        if entry.get("status") != "native":
            continue
        native = entry.get("native") or {}
        artifact_path: str | None = native.get("artifact_path")
        if not artifact_path:
            if native.get("provider"):
                # bakery was supposed to resolve this provider but didn't — a real gap.
                log.warning(
                    "binary %s status=native provider=%s but artifact_path unset "
                    "— skipping shadow",
                    entry.get("path"), native.get("provider"),
                )
            # else: already-native FreeBSD/script binary — nothing to substitute,
            # no native block was ever expected (see probe.probe: native is only
            # proposed for abi=="linux"), so this is the normal case, not a warning.
            continue

        if base_prefix is None:
            log.debug(
                "binary %s: no bakery base for this run — using image's own binary",
                entry.get("path"),
            )
            continue

        in_jail_path = base_prefix.rstrip("/") + "/" + artifact_path.lstrip("/")

        # Artifact-reality invariant (ARCHITECTURE.md): verify the resolved path
        # actually exists (through the mount) before ever shadowing it.
        if not (Path(rootfs_path) / in_jail_path.lstrip("/")).exists():
            log.debug(
                "binary %s: native artifact %s not in bakery base — skipping shadow "
                "(falls back to image's own binary)",
                entry.get("path"), in_jail_path,
            )
            continue

        basename = Path(artifact_path).name
        link = shadow_dir / basename
        # Also create an alias matching the image's binary basename if different.
        image_basename = Path(entry["path"]).name

        for name in {basename, image_basename}:
            target = link.parent / name
            if target.exists() or target.is_symlink():
                log.debug("shadow link already exists: %s", target)
                continue
            # Symlink target is the absolute in-jail path, through the base mount.
            target.symlink_to(in_jail_path)
            log.debug("shadow link: %s -> %s", target, in_jail_path)
    # END_POPULATE_SHADOW_SYMLINKS
# _assemble_native_shadow:end


# _needs_linuxulator: returns True iff manifest.linuxulator.required is set OR
#                     any binary entry has status == "linuxulator" (pure, no IO)
def _needs_linuxulator(manifest: dict[str, Any]) -> bool:
    """
    Return True iff Linuxulator must be enabled.

    Gate: manifest.linuxulator.required OR any binary has status == "linuxulator".
    Plain jails (all-native images) skip the entire linux64 kldload path.
    """
    linuxulator_block = manifest.get("linuxulator", {})
    if linuxulator_block.get("required", False):
        return True
    return any(
        b.get("status") == "linuxulator" for b in manifest.get("binaries", [])
    )


# ===========================================================================
# jail.conf generation
# ===========================================================================

# _build_jail_conf:start
#   purpose: render a minimal jail.conf text snippet for a single-run jail
#   input:
#     jail_name: str — unique name for this jail instance
#     rootfs_path: str — absolute path to the writable ZFS clone (jail path =)
#     mounts: list[str] — pre-formatted fstab lines to add via mount +=
#     extra_params: list[str] — additional raw jail.conf parameter lines
#     linuxulator: bool — when True, adds linprocfs/linsysfs/tmpfs/fdescfs mounts
#     network: str — "none" (default; ip4/ip6 disabled — no network in the jail) or
#              "inherit" (shares the host's network stack; opt-in only, for commands
#              that genuinely need registry/package access at exec time)
#     allow_raw_sockets: bool — default False; only set True when the command actually
#              needs raw sockets (e.g. ping-like diagnostics) — plain jails have no
#              VNET isolation, so a false-True default let every sandboxed build send
#              raw packets on the host's network stack for no reason
#     devfs_delegation_ok: bool — default True; meaningless unless _is_jailed() is
#              also True. Set to the real result of _devfs_delegation_available() by
#              the caller. True (or non-nested) means `mount.devfs;` is left to its
#              natural ruleset-4 default; False means the outer jail can't delegate
#              ruleset 4, so this function must force the explicit, opt-in-only
#              ruleset 0 fallback instead (caller has already verified
#              allow_unrestricted_devfs before getting here in that case).
#   output:
#     conf_text: str — complete jail.conf snippet as a string (not written to disk here)
#   sideEffects: none
#   rationale: uses mount += per-mount (additive) to avoid the mount.fstab footgun
#              that would replace the entire fstab and silently break subsequent mounts
def _build_jail_conf(
    *,
    jail_name: str,
    rootfs_path: Path | str,
    mounts: list[str],
    extra_params: list[str],
    linuxulator: bool,
    network: str = "none",
    allow_raw_sockets: bool = False,
    ip4_addr: str | None = None,
    epair_if: str | None = None,
    devfs_delegation_ok: bool = True,
) -> str:
    """
    Render a minimal jail.conf snippet for this run.

    Uses `mount +=` (additive) per-mount so each -v bind is a separate
    parameter — avoids the mount.fstab footgun that resets the whole fstab.
    [GOTCHA] mount += is additive; mount.fstab replaces.

    [SECURITY] Default-deny network. jailrun's jails are plain
    (no VNET), so a jail either shares the host's IP stack (`ip4=inherit`) or has
    none at all (`ip4=disable`) — there is no partial/firewalled-outbound option
    without VNET + a per-jail pf/ipfw anchor, which is a separate, larger change
    (see ARCHITECTURE.md open item). Until that lands, default to no network at
    all inside the run-jail: package/toolchain provisioning already happens on the
    HOST side (store.resolve()'s skopeo pull, bakery's pkg/port installs into the
    base dataset) before the jail is ever created, so the example native-first
    esphome compile needs no network during `jexec`. Callers that genuinely need
    network at exec time must opt in explicitly (`--network inherit` in cli.py).
    """
    if network not in ("none", "inherit", "allow", "vnet"):
        raise ValueError(
            f"network must be 'none', 'inherit', 'allow', or 'vnet', got {network!r}"
        )
    if network == "allow" and not ip4_addr:
        # Destination-scoped egress (0.4) needs a dedicated alias IP to tag+NAT.
        raise ValueError("network='allow' requires ip4_addr (the jail's alias IP)")
    if network == "vnet" and not epair_if:
        raise ValueError("network='vnet' requires epair_if (the jail-side epair name)")

    lines: list[str] = [
        f"{jail_name} {{",
        # rootfs_path is a Path (store.clone()'s real return type) — repr()'ing it
        # directly renders "PosixPath('/...')" into jail.conf, not a quoted string.
        # str() first so !r produces a normal quoted path.
        f"    path = {str(rootfs_path)!r};",
        "    persist;",                         # needed when we jexec rather than exec.start
        "    mount.devfs;",                     # /dev inside jail
    ]

    # [NESTED-JAIL devfs] `mount.devfs;` defaults to devfs_ruleset 4
    # (devfsrules_jail), which correctly restricts the run-jail's /dev to a
    # safe minimal set (null/zero/random/...) — on the bare host AND when
    # nested, PROVIDED the jail jailrun itself runs inside was created with
    # `allow.mount allow.mount.devfs enforce_statfs=1 devfs_ruleset=4
    # children.max>=1` (empirically confirmed 2026-07-26 — see
    # _devfs_delegation_available()'s docstring and CHANGELOG). If the outer
    # jail lacks that delegation, the identical `mount.devfs;` fails outright
    # with EPERM at `jail -c` time rather than silently degrading — so the
    # only case needing special handling here is the explicit, already
    # opted-in (--allow-unrestricted-devfs) fallback.
    #
    # [SECURITY] ruleset 0 is the UNRESTRICTED devfs: a fresh devfs mounted
    # inside a jail exposes the FULL host device set (mem, kmem, raw disks,
    # ...). Only ever selected here when the caller has already confirmed
    # devfs_delegation_ok is False AND the operator explicitly passed
    # --allow-unrestricted-devfs (see the run() guard) — never a silent
    # default.
    if _is_jailed() and not devfs_delegation_ok:
        lines.append("    devfs_ruleset = 0;")   # explicit opt-in fallback only
        log.warning(
            "jailrun is running inside a jail that does not delegate devfs "
            "ruleset 4 (missing allow.mount.devfs/enforce_statfs/children.max "
            "on the outer jail): nested run-jail /dev uses the UNRESTRICTED "
            "devfs ruleset 0. Safe for trusted workloads; HARDEN before "
            "running untrusted code -- configure the outer jail instead (see "
            "ROADMAP.md) so this warning stops firing."
        )

    if allow_raw_sockets:
        lines.append("    allow.raw_sockets;")

    if network == "none":
        lines += ["    ip4 = disable;", "    ip6 = disable;"]
    elif network == "vnet":
        lines += [
            "    vnet;",
            f"    vnet.interface = {epair_if!r};",
        ]
    elif network == "allow":
        # Destination-scoped (0.4, A2 shared-stack): the jail gets its alias
        # IP plus ip4=inherit so the routing table (including the default route)
        # is visible — the jail needs to send packets through the host stack for
        # the pf NAT/anchor to work. pf tags+NATs its egress to the allowed
        # destinations (engine.py applies the sub-anchor before jail -c). ip6
        # stays disabled so traffic can't sneak out v6 around the v4 filter.
        lines.append(f"    ip4.addr = {ip4_addr!r};")
        lines.append("    ip4 = inherit;")
        lines.append("    ip6 = disable;")
    else:
        lines += ["    ip4 = inherit;", "    ip6 = inherit;"]

    if linuxulator:
        # linprocfs and linsysfs are Linux ABI pseudo-filesystems.
        # fdescfs with -o linrdlnk patches /proc/self/fd symlink resolution.
        # tmpfs on /dev/shm (mode 1777) is required by many Linux runtimes.
        # [GOTCHA] fdescfs -o linrdlnk required for Linuxulator /proc/self/fd.
        # [GOTCHA] the mountpoint field in a jail.conf `mount +=` fstab line is an
        # ABSOLUTE HOST PATH, not resolved against the jail's own `path=` — unlike
        # what a bare "/sys" might suggest. Confirmed live 2026-07-19:
        # jail(8) tried `mount -t linsysfs linsysfs /sys` verbatim against
        # the HOST's own /sys, which doesn't exist, and failed jail creation
        # outright. Must prefix with rootfs_path, exactly like the nullfs bind
        # lines below already do.
        rootfs_str = str(rootfs_path)
        lines += [
            f"    mount += 'linprocfs {rootfs_str}/proc linprocfs rw 0 0';",
            f"    mount += 'linsysfs {rootfs_str}/sys linsysfs rw 0 0';",
            f"    mount += 'tmpfs {rootfs_str}/dev/shm tmpfs rw,size=134217728,mode=1777 0 0';",
            f"    mount += 'fdescfs {rootfs_str}/dev/fd fdescfs rw,linrdlnk 0 0';",
        ]

    for m in mounts:
        # Each mount is already formatted as an fstab line.
        lines.append(f"    mount += {m!r};")

    for p in extra_params:
        lines.append(f"    {p}")

    lines.append("}")
    return "\n".join(lines)
# _build_jail_conf:end


# ===========================================================================
# Async subprocess helpers
# ===========================================================================

# Default ceiling for short administrative commands (kldload, jail -c/-r, rctl, sysctl).
# The actual (potentially multi-minute) build runs through _stream_jexec, which has its
# own, much longer, timeout — see DEFAULT_JEXEC_TIMEOUT_S below.
DEFAULT_SUBPROCESS_TIMEOUT_S = 60.0

# Per-jail VNET epair counter — incremented for each VNET jail so epair names
# (epair0, epair1, …) don't collide during concurrent runs.
_vnet_epair_counter = 0
_vnet_epair_lock: Any = None
try:
    import threading
    _vnet_epair_lock = threading.Lock()
except ImportError:
    pass


# _run_subprocess:start
#   purpose: run an external command asynchronously, collect stdout/stderr,
#            optionally raise on non-zero exit or timeout
#   input:
#     argv: list[str] — command and arguments passed to asyncio.create_subprocess_exec
#     check: bool — when True, raises RuntimeError if returncode != 0 or on timeout/spawn failure
#     timeout: float | None — seconds before killing the process; None = no limit
#   output:
#     result: tuple[int, str, str] — (returncode, stdout, stderr) all decoded as UTF-8;
#             on timeout or spawn failure with check=False: (-1, "", <error message>)
#   sideEffects: spawns subprocess via asyncio.create_subprocess_exec with argv[0]
#                as the executable; logs full argv at DEBUG; logs stderr at DEBUG;
#                kills the process on timeout
async def _run_subprocess(
    argv: list[str],
    *,
    check: bool = True,
    timeout: float | None = DEFAULT_SUBPROCESS_TIMEOUT_S,
) -> tuple[int, str, str]:
    """
    Run a subprocess, streaming stderr to the logger, capturing stdout.
    Returns (returncode, stdout, stderr). Never hangs forever:
    a missing binary or an expired timeout is tolerated (returns rc=-1) when
    check=False, and raises a clear RuntimeError when check=True.
    """
    log.debug("exec: %s", shlex.join(argv))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # e.g. binary not found — a best-effort (check=False) caller must not crash.
        if check:
            raise RuntimeError(f"Command failed to start: {shlex.join(argv)}: {exc}") from exc
        log.warning("command failed to start (tolerated): %s: %s", shlex.join(argv), exc)
        return -1, "", str(exc)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("command timed out after %ss, killing: %s", timeout, shlex.join(argv))
        proc.kill()
        await proc.wait()
        if check:
            raise RuntimeError(f"Command timed out after {timeout}s: {shlex.join(argv)}")
        return -1, "", f"timed out after {timeout}s"

    rc = proc.returncode
    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    if stderr:
        log.debug("stderr: %s", stderr.rstrip())
    if check and rc != 0:
        raise RuntimeError(
            f"Command failed (rc={rc}): {shlex.join(argv)}\nstderr: {stderr}"
        )
    return rc, stdout, stderr
# _run_subprocess:end


# ===========================================================================
# rctl resource limits
# ===========================================================================

# Provisional defaults — NOT yet fully profiled against a real esphome/platformio
# build. Sized generously so a legitimate parallel toolchain build doesn't get
# killed; must be re-verified together with a real compile and
# tightened once real numbers are known.
#
# [GOTCHA] `deny` only means something for resources rctl(8) can check AT THE
# MOMENT of the action (maxproc: refuse a new fork; openfiles: refuse a new
# open). For ACCUMULATING/rate resources (cputime, memoryuse, pcpu, disk I/O)
# there is nothing to "deny" — usage just keeps rising — so `deny` is a silent
# no-op for them. Confirmed live 2026-07-19: a jail with
# `cputime:deny=2` ran a CPU-bound busy-loop straight through to the OUTER
# --timeout (30s) completely unaffected; the SAME rule with `cputime:sigkill=2`
# correctly killed it (exit 247 = terminated by SIGKILL) within ~2s.
#
# [STRESS-TESTED 2026-07-20, individually, against deliberately-bad input on
# a FreeBSD dev host with kern.racct.enable=1 live] memoryuse:sigkill — CONFIRMED: a
# doubling-string memory bomb capped at memoryuse:sigkill=100m died (exit 247)
# around iteration 22-23 (~130-260MB), never reaching the 40-iteration
# "SURVIVED_ALL" marker. maxproc:deny — CONFIRMED: with maxproc:deny=10, a loop
# forking 50 background sleeps stopped forking after exactly 9 (correctly
# refusing the 10th, counting the shell itself). writebps:throttle — CONFIRMED:
# a 50MB dd write took 10.53s under writebps:throttle=5m (4.7MB/s, matching the
# limit) vs 0.01s / 4.6GB/s unthrottled — ~1000x difference.
#
# pcpu:sigkill — NOT CONFIRMED, likely non-functional as configured. A
# single-threaded busy-loop under pcpu:sigkill=10 (10x below actual usage) ran
# for 60s+ without being killed; `rctl -u` on the live jail showed
# `pcpu=100` (kernel IS tracking usage correctly, 10x over the sigkill
# threshold) yet no signal was ever delivered. Kept in the defaults below
# (harmless — cputime:sigkill is the real backstop against runaway CPU use;
# pcpu adds nothing today but costs nothing either) but do NOT rely on it, and
# don't advertise CPU-runaway protection as "pcpu-enforced" anywhere. Root
# cause not diagnosed (FreeBSD version quirk vs a jailrun rule-string bug) —
# worth a closer look before ever leaning on it specifically.
DEFAULT_RCTL_RULES: tuple[str, ...] = (
    "memoryuse:sigkill=8g",  # aggregate resident memory across the whole jail — CONFIRMED working (see above)
    "pcpu:sigkill=400",      # up to ~4 cores continuously — NOT CONFIRMED, likely inert (see above); cputime below is the real backstop
    "maxproc:deny=512",      # admission control — CONFIRMED working (see above)
    "cputime:sigkill=3600",  # aggregate CPU-seconds backstop against runaway loops — CONFIRMED working (see above)
    "readbps:throttle=200m", # disk read throughput ceiling — NOTE: confirmed INERT on FreeBSD 15.1-RC2 (rctl accepts the rule but reads complete unthrottled regardless of filesystem or caching); kept in defaults because it's harmless
    "writebps:throttle=200m",# disk write throughput ceiling — CONFIRMED working (see above)
)


# _racct_enabled:start
#   purpose: check whether FreeBSD's racct/rctl resource-limiting subsystem is
#            active on this host (kern.racct.enable is a boot-time loader tunable)
#   input: none
#   output:
#     enabled: bool — True iff `sysctl -n kern.racct.enable` reports "1"
#   sideEffects: runs 'sysctl -n kern.racct.enable'
#   rationale: racct cannot be toggled at runtime; if off, every `rctl -a` call
#              would fail — check once and degrade gracefully (mirrors the
#              lifecycle NotAvailable pattern) instead of spamming failures
async def _racct_enabled() -> bool:
    rc, out, _err = await _run_subprocess(["sysctl", "-n", "kern.racct.enable"], check=False)
    return rc == 0 and out.strip() == "1"
# _racct_enabled:end


# _apply_rctl:start
#   purpose: apply rctl resource-limit rules to a running jail, best-effort
#   input:
#     jail_name: str — name of an already-created jail (rctl subject "jail:<jail_name>")
#     rules: tuple[str, ...] — rctl action strings without the "jail:<name>:" prefix,
#            e.g. "pcpu:deny=400"
#   output: none
#   sideEffects: if racct is enabled, runs 'rctl -a jail:<jail_name>:<rule>' per rule
#                (check=False — one bad/unsupported rule must not abort the run);
#                logs a WARNING once and skips entirely if racct is disabled
def _rctl_subject(jail_name: str) -> str:
    return f"jail:{jail_name}"


async def _apply_rctl(jail_name: str, rules: tuple[str, ...]) -> None:
    if not rules:
        return
    if not await _racct_enabled():
        log.warning(
            "kern.racct.enable=0 (or sysctl unavailable) — rctl limits NOT applied "
            "to %s; enable racct in /boot/loader.conf for resource-limited jails",
            jail_name,
        )
        return
    subject = _rctl_subject(jail_name)
    for rule in rules:
        rc, _out, err = await _run_subprocess(["rctl", "-a", f"{subject}:{rule}"], check=False)
        if rc != 0:
            log.warning("rctl -a %s:%s failed (rc=%d): %s", subject, rule, rc, err.strip())
        else:
            log.debug("rctl applied: %s:%s", subject, rule)
# _apply_rctl:end


# _clear_rctl:start
#   purpose: remove all rctl rules for a jail during teardown, best-effort
#   input:
#     jail_name: str — jail name whose "jail:<jail_name>" rctl rules should be removed
#   output: none
#   sideEffects: runs 'rctl -r jail:<jail_name>' (check=False — a missing racct or
#                already-cleared ruleset is fine, never fatal to teardown)
async def _clear_rctl(jail_name: str) -> None:
    subject = _rctl_subject(jail_name)
    rc, _out, err = await _run_subprocess(["rctl", "-r", subject], check=False)
    if rc != 0:
        log.debug("rctl -r %s: rc=%d (%s) — likely no rules were active", subject, rc, err.strip())
# _clear_rctl:end


# The actual build/compile runs here — esphome/platformio can genuinely take several
# minutes (first-time toolchain resolution, large ninja/make graphs). Default is
# generous; callers can override per-run via opts["timeout"] — a
# wedged build must be killable, not hang forever, but must not be strangled either).
DEFAULT_JEXEC_TIMEOUT_S = 1800.0  # 30 minutes


# ===========================================================================
# Run log persistence (`jailrun logs`) — env-var reader + best-effort open
# ===========================================================================

# JAILRUN_LOG_DIR controls where per-run captured stdout/stderr logs are
# written (see _open_log_file() and _run_async's log-file-setup, below).
# Mirrors the JAILRUN_ZPOOL/JAILRUN_MOUNTPOINT_BASE env-var-reader pattern in
# store.py's _get_zpool()/_get_mountpoint_base() — kept here (not in rundb.py)
# because engine.py is where the log file itself is opened/written; rundb.py
# only stores the resulting path in its log_path column.
DEFAULT_LOG_DIR = "/var/log/jailrun"


# _get_log_dir: reads JAILRUN_LOG_DIR env var, returns log directory path (no validation; created lazily by _open_log_file at write time)
def _get_log_dir() -> str:
    """Return the log directory root from JAILRUN_LOG_DIR; default '/var/log/jailrun'."""
    return os.environ.get("JAILRUN_LOG_DIR", DEFAULT_LOG_DIR).strip()


# _open_log_file:start
#   purpose: best-effort open (creating the parent directory first) of a
#            run's per-jail log file, for _run_async to tee _stream_jexec's
#            stdout/stderr into alongside the existing live streaming
#   input:
#     log_path: str — absolute path the log should be written to (see
#               _get_log_dir(); caller builds this as
#               '<_get_log_dir()>/<jail_name>.log')
#   output:
#     fh: (a binary file object opened in "wb" mode) | None — None if the
#         parent directory or file could not be created/opened
#   sideEffects: os.makedirs(parent dir, exist_ok=True); open(log_path, "wb").
#                NEVER raises — any OSError is logged as a warning and None is
#                returned, so the caller can continue with live streaming only
#                (see _run_async's log-file-setup: recording run state / run
#                output must never crash an actual jail run, same rationale as
#                RunDB.record_start()/record_exit()).
def _open_log_file(log_path: str):
    """Best-effort open of a run's log file. Returns None (never raises) on failure."""
    try:
        parent = os.path.dirname(log_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return open(log_path, "wb")  # noqa: SIM115 (closed explicitly by the caller)
    except OSError as exc:
        log.warning(
            "could not open log file %s (continuing with live streaming only, no persisted log): %s",
            log_path, exc,
        )
        return None
# _open_log_file:end


# _stream_jexec_pty:start
#   purpose: run jexec with a pseudo-terminal for interactive (-it) mode
#   input: same as _stream_jexec (jail_name, cmd, env, workdir, timeout)
#   output: exit_code: int
#   sideEffects: allocates a PTY with os.openpty/pty.fork; forwards stdin
#                and PTY master in both directions; handles terminal resize
#                via SIGWINCH forwarding
#   rationale: Docker's -it flag allocates a PTY so the jailed process sees
#              a real terminal (TERM, job control, ctrl-c forwarding). Without
#              a PTY, interactive tools (editors, shells with line editing)
#              behave differently or fail. This function provides that by
#              running jexec through a PTY slave and forwarding the master
#              side's I/O.
def _stream_jexec_pty(
    jail_name: str,
    cmd: list[str],
    *,
    env: dict[str, str],
    workdir: str | None,
    timeout: float | None = DEFAULT_JEXEC_TIMEOUT_S,
    log_file: Any = None,
) -> int:
    """
    Run jexec with a PTY for interactive (-it) mode. Blocks until the
    jailed process exits. Returns exit code.

    Does NOT use asyncio — PTY I/O is inherently blocking. The caller
    (asyncio.to_thread) runs this in a thread pool when interactive is set.
    """
    import os  # noqa: PLC0415
    import pty  # noqa: PLC0415
    import shlex  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import termios  # noqa: PLC0415
    import time  # noqa: PLC0415
    import tty  # noqa: PLC0415

    # Build jexec argv — mirrors _stream_jexec's logic
    env_prefix: list[str] = ["env"]
    for k, v in env.items():
        env_prefix.append(f"{k}={v}")

    jexec_argv: list[str] = ["jexec", jail_name]
    if workdir:
        inner = shlex.join(env_prefix + cmd)
        jexec_argv += ["sh", "-c", f"cd {shlex.quote(workdir)} && {inner}"]
    else:
        jexec_argv += env_prefix + cmd

    log.debug("jexec (pty): %s", shlex.join(jexec_argv))

    # Create a PTY pair and attach the slave to the jexec process.
    master_fd, slave_fd = pty.openpty()

    proc = subprocess.Popen(
        jexec_argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    # Set stdin to raw mode so keystrokes are forwarded as-is
    try:
        old_tty = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin)
    except Exception:  # noqa: BLE001 — non-TTY stdin is fine (e.g. piped input)
        old_tty = None

    # Set master PTY to non-blocking so we can check for stdin too
    os.set_blocking(master_fd, False)

    start_time = time.monotonic()
    try:
        while True:
            # Check timeout
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed > timeout:
                    log.warning(
                        "jexec (pty) in jail %s timed out after %ss — removing jail",
                        jail_name, timeout,
                    )
                    proc.kill()
                    break

            # Read from PTY master (non-blocking)
            try:
                data = os.read(master_fd, 65536)
                if data:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                    if log_file is not None:
                        try:
                            log_file.write(data)
                            log_file.flush()
                        except OSError:
                            log_file = None
            except BlockingIOError:
                pass

            # Read from stdin and forward to PTY (non-blocking)
            try:
                stdin_data = os.read(sys.stdin.fileno(), 65536)
                if stdin_data:
                    os.write(master_fd, stdin_data)
            except (BlockingIOError, OSError):
                pass

            # Check if process exited
            rc = proc.poll()
            if rc is not None:
                exit_code = rc
                break

            time.sleep(0.05)

    except KeyboardInterrupt:
        try:
            os.write(master_fd, b"\x03")
        except OSError:
            pass
    finally:
        if old_tty is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSANOW, old_tty)
            except Exception:  # noqa: BLE001
                pass
        os.close(master_fd)
        proc.wait()
        exit_code = proc.returncode if proc.returncode is not None else exit_code

    return exit_code
# _stream_jexec_pty:end


# _stream_jexec:start
#   purpose: execute a command inside a running jail via jexec(8) and stream
#            its stdout/stderr line-by-line to the process's own stdout/stderr
#            (and, best-effort, tee the same bytes into a persisted log file)
#   input:
#     jail_name: str — name of an already-running jail (started with jail -c)
#     cmd: list[str] — command and arguments to run inside the jail
#     env: dict[str, str] — environment variables prepended via `env KEY=VALUE`
#     workdir: str | None — if set, cd to this path inside the jail before exec
#     timeout: float | None — seconds before killing the jailed process; None = no limit
#     conf_path: str | None — the jail.conf used to `-c` this jail; passed to an
#                emergency `jail -f <conf_path> -r` on timeout so mount += entries
#                get unmounted the same way normal teardown does (see rationale)
#     log_file: an already-open binary file object (opened by _run_async via
#               _open_log_file()), or None — when given, every chunk read from
#               the jailed process's stdout/stderr is ALSO written here (tee),
#               on top of the existing live streaming; this function does NOT
#               open or close it (see _run_async, which owns that lifecycle)
#     on_process_started: an optional callable invoked with the live
#               asyncio.subprocess.Process right after it is spawned (before
#               any streaming/wait begins), or None (default) — added for
#               exec_run()/_exec_async() (`jailrun exec`) to capture the PID so
#               a signal received by jailrun's OWN CLI process can be forwarded
#               to this specific jexec'd process (see _install_signal_forwarding).
#               _run_async's own call site never passes this (defaults to None,
#               so `jailrun run`'s behavior is completely unchanged).
#   output:
#     exit_code: int — exact returncode of the process that ran inside the jail
#   sideEffects: spawns subprocess `jexec <jail_name> [sh -c 'cd <workdir> && ]
#                env KEY=VALUE ... <cmd>`; pipes jail process stdout line-by-line
#                to sys.stdout.buffer; pipes jail process stderr line-by-line to
#                sys.stderr.buffer; on timeout, removes the WHOLE JAIL (not just
#                the jexec'd process); if log_file is given, writes the same
#                bytes to it too — a write failure there is caught, logged as a
#                warning, and DISABLES further log writes for the rest of this
#                call (live streaming to stdout/stderr is never affected); calls
#                on_process_started(proc) once, right after spawn, iff given
#   rationale: jexec is used instead of jail exec.start because exec.start returns
#              rc=0 on successful dispatch regardless of the inner command's exit code
async def _stream_jexec(
    jail_name: str,
    cmd: list[str],
    *,
    conf_path: str | None = None,
    env: dict[str, str],
    workdir: str | None,
    timeout: float | None = DEFAULT_JEXEC_TIMEOUT_S,
    log_file: Any = None,
    on_process_started: Callable[[Any], None] | None = None,
) -> int:
    """
    Execute `cmd` inside `jail_name` via jexec; stream stdout+stderr to our
    own stdout/stderr line-by-line.

    [GOTCHA] We use jexec, NOT jail exec.start, because exec.start returns 0
    on successful dispatch regardless of the command's own exit code.  jexec
    propagates the inner process returncode faithfully.

    env is passed via `jexec -l` (login environment) + explicit KEY=VALUE
    prepended to the command so they are seen by the target binary.

    If log_file is given (an already-open binary file, see _run_async), the
    same bytes streamed to stdout/stderr are ALSO written there (tee) so
    `jailrun logs <jail_name>` can retrieve them later. A failure to write to
    log_file is swallowed (logged as a warning, further log writes disabled
    for this call) — it must never affect the live streaming or the run.

    Raises RuntimeError if the jailed process exceeds `timeout` — the process is
    killed first so nothing is left running inside the jail.
    """
    # START_BUILD_JEXEC_ARGV
    # Build environment prefix: `env KEY=VALUE ...` prepended.
    env_prefix: list[str] = ["env"]
    for k, v in env.items():
        env_prefix.append(f"{k}={v}")

    # Build the full jexec argv.
    jexec_argv: list[str] = ["jexec", jail_name]

    # Optionally cd to workdir inside the jail via sh -c.
    if workdir:
        inner = shlex.join(env_prefix + cmd)
        jexec_argv += ["sh", "-c", f"cd {shlex.quote(workdir)} && {inner}"]
    else:
        jexec_argv += env_prefix + cmd

    log.debug("jexec: %s", shlex.join(jexec_argv))
    # END_BUILD_JEXEC_ARGV

    # START_SPAWN_AND_STREAM_JEXEC
    # stdin=DEVNULL: `-it`/interactive is an explicit stub (not implemented, see
    # cli.py), so the jailed process must never blindly inherit jailrun's OWN
    # stdin. Confirmed live 2026-07-19: when invoked through the
    # bsdOS guest-agent's EXEC transport, the parent's stdin fd is not a normal
    # terminal/pipe the child can use — the jailed Python interpreter crashed
    # outright ("Fatal Python error: init_sys_streams... Bad file descriptor")
    # trying to initialize sys.stdin from it.
    proc = await asyncio.create_subprocess_exec(
        *jexec_argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # [EXEC SIGNAL FORWARDING] Hand the live Process to the caller (only
    # exec_run()/_exec_async() passes this) BEFORE any streaming/wait begins,
    # so a SIGINT/SIGTERM received by jailrun's own CLI process can be
    # forwarded straight to THIS specific jexec'd process — see
    # _install_signal_forwarding. _run_async never passes on_process_started,
    # so `jailrun run` is byte-for-byte unaffected by this hook's existence.
    if on_process_started is not None:
        on_process_started(proc)

    async def _pipe_lines(stream: asyncio.StreamReader, sink: Any) -> None:
        # _pipe_lines: drains stream in raw chunks into sink.buffer, best-effort tee to log_file (pure IO pump, no return value)
        # Chunk-based (stream.read), not line-based (readline): a build tool's
        # progress output (e.g. a `\r`-updated bar with no `\n` for a long
        # stretch) can exceed asyncio's default 64KB readline() buffer limit —
        # confirmed live 2026-07-19: a real esphome/platformio
        # compile crashed the whole run with "ValueError: Separator is not found,
        # and chunk exceed the limit". We only need passthrough streaming here,
        # not line boundaries, so chunked reads have no such limit at all.
        nonlocal log_file
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            sink.buffer.write(chunk)
            sink.buffer.flush()
            # [DEFENSE IN DEPTH] Tee the same bytes into the persisted log
            # file, if one is open. A write failure here (disk full, fd
            # closed out from under us, ...) must NEVER interrupt the live
            # streaming above or the run itself — log it once and stop
            # trying for the rest of this call.
            if log_file is not None:
                try:
                    log_file.write(chunk)
                    log_file.flush()
                except OSError as exc:
                    log.warning(
                        "log file write failed for jail %s (continuing with live "
                        "streaming only, no further persisted log for this run): %s",
                        jail_name, exc,
                    )
                    log_file = None

    import sys  # noqa: PLC0415

    async def _run_to_completion() -> int:
        await asyncio.gather(
            _pipe_lines(proc.stdout, sys.stdout),  # type: ignore[arg-type]
            _pipe_lines(proc.stderr, sys.stderr),  # type: ignore[arg-type]
        )
        await proc.wait()
        # proc.returncode is the exact exit code from the process inside the jail.
        return proc.returncode  # type: ignore[return-value]

    try:
        return await asyncio.wait_for(_run_to_completion(), timeout=timeout)
    except asyncio.TimeoutError:
        # [GOTCHA] proc.kill() alone is NOT enough. `jexec <jail> /bin/sh -c
        # 'cmd1; cmd2; cmd3'` forks a new child process per command in the
        # sequence — SIGKILL to the tracked jexec/sh PID does not cascade to
        # its own children (basic Unix signal semantics: a dead parent does not
        # kill its children, they're just orphaned/re-parented to init). Confirmed
        # live 2026-07-19: a deliberately-hung `sleep 300` kept
        # running INSIDE the jail, still jailed, long after proc.kill() "killed"
        # the wrapper — for exactly the kind of untrusted/runaway build this
        # timeout exists to stop. `jail -r` kills by JAIL MEMBERSHIP at the
        # kernel level, independent of process ancestry, so remove the whole
        # jail here instead of trusting the process tree.
        log.warning(
            "jexec in jail %s timed out after %ss — removing the jail "
            "(proc.kill() alone would not reach orphaned grandchildren)",
            jail_name, timeout,
        )
        proc.kill()
        try:
            # Bounded: don't trust this to return promptly. Confirmed live
            # 2026-07-19 — with an orphaned grandchild still running inside the
            # jail, `await proc.wait()` for the (already SIGKILLed) jexec/sh
            # wrapper itself hung indefinitely (kernel stack showed the event
            # loop parked in kqread) — the ONE thing standing between "timeout
            # detected" and "jail -r actually runs" must never itself be
            # unbounded, or the whole timeout mechanism is pointless.
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
        jail_r_argv = (
            ["jail", "-f", conf_path, "-r", jail_name]
            if conf_path is not None
            else ["jail", "-r", jail_name]
        )
        await _run_subprocess(jail_r_argv, check=False)
        raise RuntimeError(
            f"jexec in jail {jail_name} timed out after {timeout}s; jail removed"
        )
    # END_SPAWN_AND_STREAM_JEXEC
# _stream_jexec:end


# ===========================================================================
# Main orchestration
# ===========================================================================

# run:start
#   purpose: synchronous public entry point — bridges asyncio boundary for cli.py
#   input:
#     image_ref: str — OCI image reference (e.g. "debian:bookworm" or "oci:path")
#     cmd: list[str] — command to run inside the jail; empty list defaults to /bin/sh
#     opts: dict[str, Any] — runtime options: rm (bool), volumes (list of
#           (host_path, ctr_path, readonly) tuples), env (dict), workdir (str|None),
#           network ("none"|"inherit", default "none" — see _build_jail_conf SECURITY
#           note), allow_raw_sockets (bool, default False)
#   output:
#     exit_code: int — exact exit code returned by the command inside the jail
#   sideEffects: drives the full run lifecycle via _run_async (see _run_async sideEffects)
def run(image_ref: str, cmd: list[str], opts: dict[str, Any]) -> int:
    """
    Synchronous entry point called from cli.py.
    Delegates heavy lifting to _run_async(); wraps asyncio.run().
    """
    return asyncio.run(_run_async(image_ref, cmd, opts))
# run:end


# _run_async:start
#   purpose: full asynchronous run lifecycle from image resolution to jail teardown
#   input:
#     image_ref: str — OCI image reference
#     cmd: list[str] — command to run inside the jail (empty -> /bin/sh)
#     opts: dict[str, Any] — rm, volumes, env, workdir, network, allow_raw_sockets (see run())
#   output:
#     exit_code: int — exact exit code from the jexec'd process
#   sideEffects:
#     - calls _store_module.resolve(image_ref) and _store_module.unpack(image_id)
#     - calls _store_module.clone(snapshot_id) — creates a ZFS clone dataset
#     - calls _load_manifest() — may write <rootfs>/.jailrun/substitution-manifest.json
#     - calls _assemble_native_shadow() — creates symlinks under <rootfs>/jailrun-native/bin/
#     - calls _store_module.mount(handle, binds=volumes) — mounts nullfs bind volumes
#     - runs subprocess `kldload linux64` when Linuxulator is needed
#     - creates mountpoint directories under rootfs for Linuxulator pseudo-filesystems
#     - writes a temporary jail.conf file via tempfile.NamedTemporaryFile
#     - runs subprocess `jail -f <conf_path> -c <jail_name>` to start the jail
#     - calls _open_log_file() to best-effort open <JAILRUN_LOG_DIR>/<jail_name>.log
#       for `jailrun logs` (never raises out of this function; None on failure)
#     - calls RunDB().record_start(jail_name, ..., log_path=...) right after
#       `jail -c` succeeds (best-effort — see runtime/rundb.py; never raises
#       out of this function)
#     - runs jexec via _stream_jexec() — runs command inside the jail, teeing
#       stdout/stderr into the log file opened above (if any)
#     - closes the log file (if opened), in the finally block, regardless of
#       how the run ended
#     - runs subprocess `jail -r <jail_name>` to remove the jail (always, in finally)
#     - calls runtime.lifecycle.teardown(jail_name) (native jexec SIGKILL)
#     - calls RunDB().record_exit(jail_name, ...) in the finally block, iff a
#       record_start was ever attempted (best-effort — never raises)
#     - calls _store_module.destroy(handle) when opts.rm is True
async def _run_async(
    image_ref: str,
    cmd: list[str],
    opts: dict[str, Any],
) -> int:
    """
    Full run lifecycle:
      resolve → unpack → clone → load manifest → shadow → (kldload) →
      write jail.conf → jail -c → jexec → stream → rc → (destroy)
    """
    rm: bool = opts.get("rm", False)
    volumes: list[tuple[str, str, bool]] = opts.get("volumes", [])
    env: dict[str, str] = opts.get("env", {})
    workdir: str | None = opts.get("workdir")
    network: str = opts.get("network", "none")
    # Early validation of the network spec (parse_network_spec raises ValueError
    # on bad syntax) — done here so a malformed --network is rejected before any
    # expensive resolve/unpack/clone work. The full resolution + rendering happens
    # later once jail_name is known (see START_NETWORK_ALLOW_SETUP below).
    _network_spec: Any = None
    _jail_ip: str | None = None
    _net_rules_text: str | None = None
    # VNET state — set during START_VNET_SETUP, used during teardown
    _vnet_host_if: str | None = None
    _vnet_jail_if: str | None = None
    _vnet_host_ip: str | None = None
    _vnet_jail_ip: str | None = None
    if network.startswith("allow"):
        from runtime.netpolicy import parse_network_spec  # noqa: PLC0415
        _network_spec = parse_network_spec(network)
    elif network == "vnet":
        # VNET needs epair + IP allocation before jail -c.
        # The actual creation happens later (once jail_name is known),
        # but validate early that we're on FreeBSD with ifconfig.
        try:
            from runtime.netops import create_epair  # noqa: PLC0415
        except Exception as exc:
            raise RuntimeError(f"VNET not available on this host: {exc}") from exc
    allow_raw_sockets: bool = opts.get("allow_raw_sockets", False)
    rctl_enabled: bool = opts.get("rctl_enabled", True)
    rctl_rules: tuple[str, ...] = (
        tuple(opts["rctl_rules"]) if opts.get("rctl_rules") else DEFAULT_RCTL_RULES
    ) if rctl_enabled else ()
    jexec_timeout: float | None = opts.get("timeout", DEFAULT_JEXEC_TIMEOUT_S)
    allow_unrestricted_devfs: bool = opts.get("allow_unrestricted_devfs", False)

    # [SECURITY] devfs ruleset for nested run-jails. Empirically confirmed
    # 2026-07-26 (see _devfs_delegation_available()'s docstring): when
    # jailrun runs nested, its own run-jails get a properly restricted
    # ruleset-4 /dev for free, with no daemon and no jailrun-side privilege,
    # PROVIDED the outer jail was created with `allow.mount
    # allow.mount.devfs enforce_statfs=1 devfs_ruleset=4 children.max>=1`.
    # Only refuse (or fall back to the unrestricted, opt-in ruleset 0) when
    # that outer-jail delegation genuinely isn't there.
    devfs_delegation_ok = True
    if _is_jailed():
        devfs_delegation_ok = _devfs_delegation_available()
        if not devfs_delegation_ok and not allow_unrestricted_devfs:
            raise RuntimeError(
                "refusing to run: jailrun is nested (inside a jail) and that "
                "outer jail does not delegate devfs ruleset 4 to its own "
                "children -- this run-jail's /dev would be unrestricted "
                "(verified: /dev/mem is readable from inside such a "
                "run-jail). This is unsafe for untrusted code. Fix it at the "
                "source: recreate the OUTER jail with `allow.mount "
                "allow.mount.devfs enforce_statfs=1 devfs_ruleset=4 "
                "children.max>=1` (see ROADMAP.md) -- no jailrun-side change "
                "needed once that's set. Until then, pass "
                "opts['allow_unrestricted_devfs']=True (or "
                "--allow-unrestricted-devfs) only for trusted/manual testing."
            )

    # START_RESOLVE_AND_UNPACK
    # ------------------------------------------------------------------
    # 1. Resolve + unpack image → ZFS snapshot
    #
    # Native-first FreeBSD path: an image_ref of the form `nativebase:<name>`
    # selects a pre-provisioned FreeBSD-userland base (see store.base_snapshot)
    # as the run's ROOT rootfs, instead of pulling a Linux OCI image. This is
    # the correct base for running native FreeBSD toolchains (e.g. the esphome
    # venv python + xtensa toolchain): a Linux image (alpine/debian) has no
    # /libexec/ld-elf.so.1 or FreeBSD libc, so a native FreeBSD binary cannot
    # exec there at all. In this mode there is nothing to shadow (binaries are
    # already native) and no Linux ABI to load, so steps 3/4a/4d are skipped.
    # ------------------------------------------------------------------
    # `thinbase:<name>`: like nativebase, but instead of cloning (ZFS clone or
    # `cp -a`) a whole copied FreeBSD userland per run, nullfs-ro-binds the
    # CURRENT system's own base dirs (/bin, /lib, /libexec, /sbin, /usr)
    # straight from "/" into an EMPTY per-run rootfs. Zero copy, zero disk
    # cost beyond the run's own scratch dirs. Correct whether jailrun runs
    # nested inside some other application's jail (in which case "/" is that
    # jail's own already-provisioned FreeBSD userland) or on a bare FreeBSD
    # host (where "/" is that host's own userland) -- either way "share the
    # parent's userland" is exactly the intended semantics, not a hardcoded
    # path assumption.
    THIN_BASE_DIRS = ("/bin", "/lib", "/libexec", "/sbin", "/usr")
    native_base: str | None = None
    thin_base: str | None = None
    image_id: str | None = None
    if image_ref.startswith("nativebase:"):
        native_base = image_ref.split(":", 1)[1]
    elif image_ref.startswith("thinbase:"):
        thin_base = image_ref.split(":", 1)[1]

    if thin_base:
        log.info("thin-jail base (nullfs-shared userland): %s", thin_base)
    elif native_base:
        log.info("native FreeBSD base: %s", native_base)
        snapshot_id = _store_module.base_snapshot(native_base)
        log.info("snapshot: %s", snapshot_id)
    else:
        image_id = opts.get("image_id")
        if image_id is None:
            log.info("resolving image: %s", image_ref)
            image_id = _store_module.resolve(image_ref)
            log.info("image_id: %s", image_id)
        else:
            log.info("using pre-resolved image_id: %s", image_id)

        snapshot_id = _store_module.unpack(image_id)
        log.info("snapshot: %s", snapshot_id)
    # END_RESOLVE_AND_UNPACK

    # START_CLONE_ROOTFS
    # ------------------------------------------------------------------
    # 2. Clone → writable rootfs for this run (thinbase: an empty scratch dir
    #    + nullfs-ro binds of the base dirs, instead of a copy/clone)
    # ------------------------------------------------------------------
    if thin_base:
        rootfs_path, handle = _store_module.empty_rootfs()
        _store_module.mount(
            handle,
            binds=[(d, d, True) for d in THIN_BASE_DIRS if os.path.isdir(d)],
        )
        log.info("thin rootfs: %s (handle=%s)", rootfs_path, handle)
    else:
        rootfs_path, handle = _store_module.clone(snapshot_id)
        log.info("rootfs clone: %s (handle=%s)", rootfs_path, handle)
    # END_CLONE_ROOTFS

    exit_code = 1  # pessimistic default
    jail_name: str | None = None
    conf_path: str | None = None
    # jail_created gates record_exit(): only meaningful if record_start was
    # ever attempted (i.e. `jail -c` actually succeeded). run_completed
    # distinguishes the two terminal outcomes RunDB can express for a created
    # jail: True means _stream_jexec returned a real exit code normally
    # ("exited"); False means the jail was torn down some other way — the
    # jexec timeout path (see _stream_jexec) or any other exception raised
    # after jail creation — for which "killed" is the closest fit RunDB's
    # CHECK-constrained status column offers.
    jail_created = False
    run_completed = False
    # log_fh: the open binary file object teeing this run's stdout/stderr to
    # disk (see _open_log_file()/_stream_jexec's log_file param), or None if
    # no log file was ever opened. log_path: the path recorded into RunDB's
    # log_path column — set back to None if opening actually failed (see
    # START_LOG_FILE_SETUP below), so `jailrun logs` never points at a path
    # nothing was ever written to.
    log_fh: Any = None
    log_path: str | None = None

    try:
        # START_LOAD_MANIFEST
        # ------------------------------------------------------------------
        # 3. Load substitution manifest (probe+bakery, or cache)
        # ------------------------------------------------------------------
        if native_base or thin_base:
            # Native/thin FreeBSD base: binaries are already native, so there
            # is nothing to probe or shadow and no Linux ABI to load. An empty
            # manifest makes every downstream step (bakery mount, shadow
            # symlinks, _needs_linuxulator) a correct no-op.
            manifest = {}
            log.info(
                "native/thin base (%s): skipping manifest/shadow/Linuxulator",
                native_base or thin_base,
            )
        else:
            manifest = _load_manifest(rootfs_path, image_ref, image_id=image_id)
            log.info(
                "manifest loaded: %d binaries, linuxulator.required=%s",
                len(manifest.get("binaries", [])),
                manifest.get("linuxulator", {}).get("required"),
            )
        # END_LOAD_MANIFEST

        # START_ASSEMBLE_SHADOW_AND_ENV
        # ------------------------------------------------------------------
        # 4a. Mount the bakery-registered base (if any), then assemble the
        # native-first shadow layer. The base lives in its own ZFS/plaindir
        # snapshot, entirely separate from this run's image clone — without
        # this bind-mount, shadow symlinks would point at a host path invisible
        # from inside the jail's chroot.
        # ------------------------------------------------------------------
        bakery_snapshot_id = manifest.get("_bakery", {}).get("snapshot_id")
        base_prefix: str | None = None
        if bakery_snapshot_id:
            base_mountpoint = _store_module.base_mountpoint(bakery_snapshot_id)
            _store_module.mount(handle, binds=[(base_mountpoint, NATIVE_BASE_MOUNT, True)])
            base_prefix = NATIVE_BASE_MOUNT
            log.info("bakery base %s mounted at %s", bakery_snapshot_id, NATIVE_BASE_MOUNT)
        else:
            log.debug("no bakery base registered for this manifest (no native substitutes needed)")

        _assemble_native_shadow(rootfs_path, manifest, base_prefix=base_prefix)

        # ------------------------------------------------------------------
        # 4b. Build environment: prepend /jailrun-native/bin to PATH
        # ------------------------------------------------------------------
        base_path = env.get("PATH", "/jailrun-native/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        if "/jailrun-native/bin" not in base_path:
            base_path = f"/jailrun-native/bin:{base_path}"
        env = {**env, "PATH": base_path}
        # END_ASSEMBLE_SHADOW_AND_ENV

        # START_MOUNT_BIND_VOLUMES
        # ------------------------------------------------------------------
        # 4c. Mount -v binds via the store (nullfs)
        # ------------------------------------------------------------------
        # [GOTCHA] nullfs has no uid/gid remap; host uid numbers appear as-is
        # inside the jail.  Operators must ensure the jail user can read the
        # mounted paths or use idmap workarounds at the host layer.
        if volumes:
            _store_module.mount(handle, binds=volumes)
            log.info("mounted %d bind volume(s)", len(volumes))
        # END_MOUNT_BIND_VOLUMES

        # ------------------------------------------------------------------
        # 4c-bis. Copy /etc/resolv.conf when the jail has network access
        # ------------------------------------------------------------------
        # Without this, DNS resolution fails inside the jail for ALL images
        # (not just Alpine/musl). We copy (not bind-mount) the host's
        # resolv.conf into the jail's rootfs so the jailed process can resolve
        # hostnames. A copy is simpler and more robust than a nullfs file-level
        # mount (which has "must be same type" quirks on FreeBSD). Only done
        # when network != "none" — a no-network jail has no use for DNS.
        if network != "none":
            host_resolv = Path("/etc/resolv.conf")
            if host_resolv.exists():
                jail_etc = rootfs_path / "etc"
                jail_etc.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(host_resolv), str(jail_etc / "resolv.conf"))
                log.info("copied /etc/resolv.conf into rootfs (network=%s)", network)

        # START_LINUXULATOR_SETUP
        # ------------------------------------------------------------------
        # 4d. Conditionally load Linuxulator (linux64 kmod + filesystems)
        # ------------------------------------------------------------------
        linuxulator = _needs_linuxulator(manifest)
        if linuxulator:
            log.info(
                "Linuxulator required — kldload linux64 + ABI filesystems"
            )
            # kldload is idempotent if the module is already loaded.
            # [GOTCHA] fdescfs needs -o linrdlnk (handled in jail.conf below).
            await _run_subprocess(["kldload", "linux64"], check=False)
            # Ensure the mountpoints exist inside the rootfs.
            # rootfs_path is a Path (store.clone()'s real return type) — `+ mp`
            # (str concat) raised TypeError the first time this ever ran for real
            # (2026-07-19); use proper Path composition instead.
            for mp in ("/proc", "/sys", "/dev/shm", "/dev/fd"):
                (Path(rootfs_path) / mp.lstrip("/")).mkdir(parents=True, exist_ok=True)
        else:
            log.info("Linuxulator not needed — plain jail, no Linux ABI loaded")
        # END_LINUXULATOR_SETUP

        # START_BUILD_MOUNT_LINES
        # ------------------------------------------------------------------
        # 5. User -v volumes: already mounted, nothing left to do here
        # ------------------------------------------------------------------
        # Fixed 2026-07-19: this used to ALSO render each -v
        # volume as a jail.conf `mount +=` fstab line, on top of step 4c's
        # store.mount() call that ALREADY ran `mount_nullfs` for the same bind —
        # `jail -c` then tried to mount_nullfs the SAME host path onto the SAME
        # (already-mounted) target a second time and failed outright with
        # "Resource deadlock avoided". FreeBSD jails have no separate mount
        # namespace: a nullfs bind mounted onto rootfs_path/<ctr> before `jail -c`
        # is already part of what `path=rootfs_path` sees — no jail-side mount
        # action is needed at all for user volumes.
        # END_BUILD_MOUNT_LINES

        # START_WRITE_CONF_AND_START_JAIL
        # ------------------------------------------------------------------
        # 6. Write jail.conf and start jail
        # ------------------------------------------------------------------
        # Unique jail name using handle.id to avoid collisions with concurrent runs.
        # `handle` is the Handle DATACLASS — f"{handle}" stringifies the whole repr
        # (rootfs=PosixPath(...), dataset=..., ...), which jail(8) rejects outright
        # as an invalid parameter name. Found live 2026-07-19.
        jail_name = opts.get("jail_name") or f"jailrun-{handle.id}"
        handle.jail_name = jail_name  # so store.destroy()'s own jail -r step is live too

        # START_NETWORK_ALLOW_SETUP
        # ------------------------------------------------------------------
        # 6a. For destination-scoped network (--network allow=...), resolve the
        #     allow-list, detect the egress interface, allocate a loopback alias
        #     IP, render the pf sub-anchor rules, and apply them (ifconfig alias +
        #     pfctl load) before `jail -c`. Everything is resolved BEFORE jail
        #     creation so DNS failures or pool exhaustion abort early.
        # ------------------------------------------------------------------
        if _network_spec is not None:
            from runtime.netpolicy import resolve_allowlist, host_resolvers, alloc_jail_ip, render_pf_anchor  # noqa: PLC0415
            from runtime.netops import detect_ext_if, apply_network  # noqa: PLC0415
            resolved = resolve_allowlist(_network_spec.allow)
            resolvers = host_resolvers()
            ext_if = detect_ext_if()
            if ext_if is None:
                raise RuntimeError(
                    "network='allow' requires a default route (detect_ext_if() "
                    "returned None) — cannot determine the egress interface"
                )
            if _netpolicy_lock is not None:
                _netpolicy_lock.acquire()
            try:
                _jail_ip = alloc_jail_ip(used=_netpolicy_used_ips)
                _netpolicy_used_ips.add(_jail_ip)
            finally:
                if _netpolicy_lock is not None:
                    _netpolicy_lock.release()
            _net_rules_text = render_pf_anchor(
                jail_name=jail_name,
                jail_ip=_jail_ip,
                ext_if=ext_if,
                resolved=resolved,
                resolvers=resolvers,
            )
            apply_network(jail_name, _jail_ip, _net_rules_text)
            log.info(
                "destination-scoped network for %s: ip=%s ext_if=%s %d allow-entries %d resolvers",
                jail_name, _jail_ip, ext_if, len(resolved), len(resolvers),
            )
        # END_NETWORK_ALLOW_SETUP

        # START_VNET_SETUP
        # ------------------------------------------------------------------
        # 6b. For VNET (--network vnet), create an epair and assign IPs before
        #     jail -c. The epair jail side is specified in jail.conf's
        #     vnet.interface so jail(8) moves it into the jail automatically.
        #     Host-side IP + NAT pf anchor are set up here; the jail-side IP
        #     and default route are configured via jexec after jail -c (see
        #     START_VNET_JAIL_CONFIG below).
        # ------------------------------------------------------------------
        if network == "vnet":
            from runtime.netops import create_epair, setup_vnet_host, _alloc_vnet_ip  # noqa: PLC0415

            if _vnet_epair_lock is not None:
                _vnet_epair_lock.acquire()
            try:
                _vnet_host_if, _vnet_jail_if = create_epair()
                _vnet_host_ip, _vnet_jail_ip = _alloc_vnet_ip()
            finally:
                if _vnet_epair_lock is not None:
                    _vnet_epair_lock.release()

            setup_vnet_host(_vnet_host_if, _vnet_host_ip, jail_name)
            log.info(
                "VNET for %s: epair=(%s, %s) host=%s jail=%s",
                jail_name, _vnet_host_if, _vnet_jail_if, _vnet_host_ip, _vnet_jail_ip,
            )

        # START_PUBLISH_SETUP
        # ------------------------------------------------------------------
        # 6b-prime. For port publishing (--publish / -p), load pf rdr rules
        # that forward host ports to the jail. Only meaningful with VNET
        # (needs a jail IP to forward to).
        # ------------------------------------------------------------------
        publish_specs: list[tuple[int, int, str]] | None = opts.get("publish")
        if publish_specs and _vnet_jail_ip is not None:
            from runtime.netops import setup_publish_ports  # noqa: PLC0415
            setup_publish_ports(jail_name, _vnet_jail_ip, publish_specs)
        # END_PUBLISH_SETUP

        # Build --tmpfs mount lines for jail.conf.
        tmpfs_specs: list[str] | None = opts.get("tmpfs")
        _mount_lines: list[str] = []
        if tmpfs_specs:
            rootfs_str = str(rootfs_path)
            for spec in tmpfs_specs:
                parts = spec.split(":", 1)
                path = parts[0].lstrip("/")
                size = parts[1] if len(parts) > 1 else "128m"
                target = f"{rootfs_str}/{path}"
                _mount_lines.append(
                    f"tmpfs {target} tmpfs rw,size={size},mode=1777 0 0"
                )
            log.info("tmpfs mounts: %d (%s)", len(tmpfs_specs), ", ".join(tmpfs_specs))

        # Apply --read-only: make the ZFS clone read-only at the dataset level.
        # Done here AFTER all filesystem mutations (shadow assembly, bakery base
        # mount, resolv.conf copy, user -v volumes) but BEFORE jail -c so the
        # jail starts with a read-only root. User -v volumes (nullfs) and
        # --tmpfs mounts (jail.conf mount +=) are separate filesystems and are
        # unaffected.
        if opts.get("read_only") and handle.dataset and _store_module.backend == "zfs":
            await _run_subprocess(["zfs", "set", "readonly=on", handle.dataset], check=False)
            log.info("rootfs set read-only (%s)", handle.dataset)

        conf_text = _build_jail_conf(
            jail_name=jail_name,
            rootfs_path=rootfs_path,
            mounts=_mount_lines,
            extra_params=[],
            linuxulator=linuxulator,
            network="vnet" if _vnet_jail_if else ("allow" if _network_spec else network),
            allow_raw_sockets=allow_raw_sockets,
            ip4_addr=_vnet_jail_ip or _jail_ip,
            epair_if=_vnet_jail_if,
            devfs_delegation_ok=devfs_delegation_ok,
        )

        # Write jail.conf to a tempfile.
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".conf",
            prefix="jailrun-",
            delete=False,
        ) as cf:
            conf_path = cf.name
            cf.write(conf_text)
        log.debug("jail.conf written to %s:\n%s", conf_path, conf_text)

        # Start the jail (persist mode: jail stays up until we destroy it).
        await _run_subprocess(["jail", "-f", conf_path, "-c", jail_name])
        log.info("jail %s created", jail_name)
        jail_created = True

        # START_VNET_JAIL_CONFIG
        # ------------------------------------------------------------------
        # 6a-prime. For VNET jails, configure the jail-side epair IP and
        # default route via jexec now that the jail is running.
        # ------------------------------------------------------------------
        if _vnet_host_if is not None and _vnet_jail_ip is not None:
            from runtime.netops import setup_vnet_jail  # noqa: PLC0415
            setup_vnet_jail(jail_name, _vnet_jail_if, _vnet_jail_ip, _vnet_host_ip, linuxulator=linuxulator)
        # END_VNET_JAIL_CONFIG

        # START_LOG_FILE_SETUP
        # ------------------------------------------------------------------
        # 6a. Give this run a persisted log file so `jailrun logs <jail_name>`
        # can retrieve captured output after the fact (runtime/rundb.py's
        # log_path column + get_log_path()). Convention: <JAILRUN_LOG_DIR>/
        # <jail_name>.log (see _get_log_dir()).
        # ------------------------------------------------------------------
        # [DEFENSE IN DEPTH] _open_log_file() already catches OSError
        # internally and never raises (returns None instead) — this
        # try/except is a second layer against any other unexpected exception
        # type, same rationale as the RunDB call below: a failure to open the
        # log file must NEVER break a real jail run. If it can't be opened,
        # we fall back to live streaming only and record no log_path (a path
        # nothing was ever written to would only mislead `jailrun logs`).
        log_path = os.path.join(_get_log_dir(), f"{jail_name}.log")
        try:
            log_fh = _open_log_file(log_path)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "failed to open log file %s for %s (non-fatal, no persisted log): %s",
                log_path, jail_name, exc,
            )
            log_fh = None
        if log_fh is None:
            log_path = None
        # END_LOG_FILE_SETUP

        # Record the run start for `jailrun ps` (runtime/rundb.py). Imported
        # lazily so engine.py stays importable/testable without a real db.
        # image_digest uses snapshot_id — the one image identifier available
        # on BOTH the native-base and normal-image code paths above (image_id
        # is only bound in the normal-image branch). dataset uses handle.dataset
        # (the ZFS dataset name / plaindir path from store.clone()'s Handle),
        # not rootfs_path — either is a legitimate fit per rundb.py's own field
        # docstring ("ZFS dataset / rootfs path backing this run").
        # [DEFENSE IN DEPTH] RunDB.record_start() already catches
        # OSError/sqlite3.Error internally and never raises — this try/except
        # is a second layer against any other unexpected exception type,
        # because recording run state must NEVER break a real jail run.
        try:
            from runtime.rundb import RunDB  # noqa: PLC0415
            RunDB().record_start(
                jail_name,
                image=image_ref,
                image_digest=str(snapshot_id),
                dataset=handle.dataset,
                log_path=log_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rundb: failed to record run start for %s (non-fatal): %s", jail_name, exc)

        # rctl resource limits — best-effort, never fatal to the run.
        await _apply_rctl(jail_name, rctl_rules)
        # END_WRITE_CONF_AND_START_JAIL

        # START_JEXEC_RUN
        # ------------------------------------------------------------------
        # 7. jexec: run the command, stream output, capture exit code
        # ------------------------------------------------------------------
        if not cmd:
            # Default to sh if no command given (mirrors docker run IMAGE).
            cmd = ["/bin/sh"]

        if opts.get("interactive"):
            # PTY mode: run in a thread since PTY I/O is blocking.
            exit_code = await asyncio.to_thread(
                _stream_jexec_pty,
                jail_name,
                cmd,
                env=env,
                workdir=workdir,
                timeout=jexec_timeout,
                log_file=log_fh,
            )
        else:
            exit_code = await _stream_jexec(
                jail_name,
                cmd,
                conf_path=conf_path,
                env=env,
                workdir=workdir,
                timeout=jexec_timeout,
                log_file=log_fh,
            )
        log.info("jexec finished with exit code %d", exit_code)
        run_completed = True  # a real exit_code was produced — see record_exit below
        # END_JEXEC_RUN

    finally:
        # Close this run's log file (if one was ever opened) regardless of how
        # we got here (normal return, jexec timeout, or any other exception) —
        # best-effort, a close failure is never fatal to the run, which is
        # already over by this point either way.
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass

        # START_TEARDOWN
        # ------------------------------------------------------------------
        # 8. Teardown: stop jail; optionally destroy rootfs clone
        # ------------------------------------------------------------------
        if jail_name is not None:
            # Process teardown: native SIGKILL all PIDs via jexec (best-effort,
            # never fatal). See runtime/lifecycle.py.
            try:
                from runtime.lifecycle import teardown as _lc_teardown  # noqa: PLC0415
                await _lc_teardown(jail_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("lifecycle teardown of %s failed (non-fatal): %s", jail_name, exc)
            # Always remove the persist jail itself.
            # [GOTCHA] `jail -r <name>` WITHOUT `-f <conf_path>` does not unmount the
            # jail's own `mount +=`/`mount.devfs` entries (linprocfs/linsysfs/tmpfs/
            # fdescfs/devfs) — confirmed live 2026-07-19: every one
            # of them was still mounted under the clone after a bare `jail -r`, which
            # then made `zfs destroy` fail with "dataset is busy" every time, not just
            # on a transient race. Passing the SAME conf file used at `-c` time lets
            # jail(8) find and unmount them. conf_path can still be None if we failed
            # before writing it — bare `jail -r` is the best we can do then.
            try:
                jail_r_argv = (
                    ["jail", "-f", conf_path, "-r", jail_name]
                    if conf_path is not None
                    else ["jail", "-r", jail_name]
                )
                await _run_subprocess(jail_r_argv, check=False)
                log.info("jail %s removed", jail_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to remove jail %s: %s", jail_name, exc)
            finally:
                if conf_path is not None:
                    try:
                        os.unlink(conf_path)
                    except OSError:
                        pass
            # Clear rctl rules regardless of whether they were ever applied (best-effort).
            try:
                await _clear_rctl(jail_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to clear rctl rules for %s (non-fatal): %s", jail_name, exc)

            # Teardown destination-scoped network (0.4): flush the pf sub-anchor
            # and remove the loopback alias IP. Best-effort — must never change
            # the run's exit code (same as the lifecycle/rctl teardown above).
            # Uses _jail_ip which is only set when _network_spec is not None
            # (i.e. --network allow=... was used).
            if _jail_ip is not None:
                try:
                    from runtime.netops import teardown_network  # noqa: PLC0415
                    teardown_network(jail_name, _jail_ip)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "failed to teardown network for %s (non-fatal): %s", jail_name, exc,
                    )
                finally:
                    if _netpolicy_lock is not None:
                        _netpolicy_lock.acquire()
                    try:
                        _netpolicy_used_ips.discard(_jail_ip)
                    finally:
                        if _netpolicy_lock is not None:
                            _netpolicy_lock.release()

            # Teardown VNET (--network vnet): flush pf anchor, destroy epair.
            if _vnet_host_if is not None:
                try:
                    from runtime.netops import teardown_vnet  # noqa: PLC0415
                    teardown_vnet(_vnet_host_if, jail_name)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "failed to teardown VNET for %s (non-fatal): %s", jail_name, exc,
                    )

            # Record the run's terminal status for `jailrun ps` (runtime/rundb.py).
            # Gated on jail_created: only record an exit if a record_start was
            # ever attempted for this jail_name. run_completed=True means
            # _stream_jexec returned normally with a real exit_code ("exited");
            # False covers every other way the jail got here — the jexec
            # timeout path (which already killed the process and removed the
            # jail itself, see _stream_jexec) and any other exception raised
            # after jail creation — for which "killed" is the closest fit
            # RunDB's CHECK-constrained status column offers, and exit_code is
            # None since no real exit code was ever produced.
            # [DEFENSE IN DEPTH] same rationale as record_start above — never
            # let a broken rundb affect the run's own exit code / control flow.
            if jail_created:
                try:
                    from runtime.rundb import RunDB  # noqa: PLC0415
                    RunDB().record_exit(
                        jail_name,
                        status="exited" if run_completed else "killed",
                        exit_code=exit_code if run_completed else None,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("rundb: failed to record run exit for %s (non-fatal): %s", jail_name, exc)

        if rm:
            try:
                _store_module.destroy(handle)
                log.info("rootfs clone destroyed (--rm)")
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to destroy clone %s: %s", handle, exc)
        # END_TEARDOWN

    return exit_code
# _run_async:end


# ===========================================================================
# `jailrun exec` — run one more command against an ALREADY-RUNNING jail
# ===========================================================================
#
# Unlike run()/_run_async() (which create a brand-new jail from an image and
# tear it down again), exec targets a jail a PRIOR `jailrun run` already
# created and left running — the same shape as `docker exec`. It must NOT
# create or destroy anything: not the jail, not a ZFS clone, nothing. The
# jexec-invocation-and-streaming logic is exactly what _stream_jexec already
# does, so exec reuses it directly (see _exec_async below) rather than
# reimplementing it or routing through _run_async, whose whole job (resolve /
# unpack / clone / shadow / jail -c / ... / jail -r / destroy) is wrong for
# this case. The one adjustment made to _stream_jexec itself is the narrow,
# backward-compatible on_process_started hook added above — needed so exec
# can forward signals to the SPECIFIC jexec'd process (see
# _install_signal_forwarding), not to the whole jail (that would be wrong here:
# the jail may have OTHER things running in it, e.g. the original `jailrun
# run` command exec is reaching into).

# _list_live_jail_names:start
#   purpose: enumerate jail names currently visible on this host via jls(8) —
#            mirrors runtime/gc.py's _list_jail_names() exact subprocess
#            pattern (argv shape, timeout, `name=` regex parse). Duplicated
#            here rather than imported because this task's scope explicitly
#            excludes touching gc.py; keep the two in sync if jls's output
#            format is ever revisited.
#   input: none
#   output:
#     names: list[str] | None — jail names (possibly an empty list if
#            genuinely zero jails are running), or None if jls itself is
#            unavailable or failed (non-FreeBSD host, jail subsystem absent,
#            etc.) — NOT the same as "zero jails"
#   sideEffects: runs 'jls -n name' with a bounded timeout; never raises
def _list_live_jail_names() -> list[str] | None:
    """Return jail names from `jls -n name`, or None if jls is unavailable. Mirrors runtime/gc.py."""
    try:
        proc = subprocess.run(
            ["jls", "-n", "name"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("exec: jls unavailable: %s", exc)
        return None
    if proc.returncode != 0:
        log.debug("exec: jls exited %d: %s", proc.returncode, proc.stderr.strip())
        return None
    names: list[str] = []
    for line in proc.stdout.splitlines():
        m = re.search(r"\bname=(\S+)", line)
        if m:
            names.append(m.group(1))
    return names
# _list_live_jail_names:end


# _check_jail_live:start
#   purpose: verify jail_name is actually a live, running jail before
#            `jailrun exec` runs anything against it
#   input:
#     jail_name: str — target jail name (as shown by `jailrun ps`)
#   output:
#     tuple[bool, str] — (live, message); message is "" when live is True, a
#     clean one-line human-readable explanation when live is False (callers
#     print this directly — never a raw traceback)
#   sideEffects: calls _list_live_jail_names() (subprocess: jls -n name);
#                best-effort imports runtime.rundb.RunDB and calls
#                .list_runs(status="running") — ANY failure there (missing
#                db, OSError, sqlite3.Error, or any other exception type) is
#                caught, logged as a warning, and does NOT change the result
#   rationale: jls is ground truth for "is a jail alive on this host right
#              now". rundb is jailrun's OWN bookkeeping ledger and can be
#              stale — a crash before record_start ever ran, a 'running' row
#              left behind after an out-of-band `jail -r`, etc. (see
#              runtime/gc.py's entire reconciliation module for exactly this
#              class of drift). So precedence is: jls says the jail is NOT
#              live -> not live, full stop, even if rundb still says
#              'running'. jls says it IS live -> live; rundb is consulted
#              ONLY as best-effort corroboration (logs a warning on a
#              mismatch, or if the db is unreachable) and never overrides
#              jls in either direction — never hard-fail exec just because
#              the rundb ledger is unreachable when jls already confirmed
#              the jail is really there.
def _check_jail_live(jail_name: str) -> tuple[bool, str]:
    """Authoritative-jls + best-effort-rundb liveness check for `jailrun exec`."""
    known_jails = _list_live_jail_names()
    if known_jails is None:
        return False, (
            "jailrun exec: could not enumerate live jails via `jls -n name` "
            "on this host (not FreeBSD, or jls unavailable)"
        )
    if jail_name not in known_jails:
        return False, f"jailrun exec: no live jail named {jail_name!r} (jls -n name)"

    # jls says the jail IS live — authoritative, we proceed regardless of
    # what follows. rundb is consulted only for a best-effort warning.
    try:
        from runtime.rundb import RunDB  # noqa: PLC0415  (lazy import, same pattern as elsewhere in this module)
        rows = RunDB().list_runs(status="running")
        running_names = {row.get("jail_name") for row in rows}
        if jail_name not in running_names:
            log.warning(
                "jailrun exec: jail %r is live per `jls` but has no 'running' "
                "rundb row (stale/missing bookkeeping) — proceeding on jls alone",
                jail_name,
            )
    except Exception as exc:  # noqa: BLE001 — rundb reachability must never block exec against a jls-confirmed-live jail
        log.warning(
            "jailrun exec: rundb unreachable (%s) — proceeding on jls alone", exc,
        )

    return True, ""
# _check_jail_live:end


# _install_signal_forwarding:start
#   purpose: install SIGINT/SIGTERM handlers for the duration of `jailrun
#            exec` so a signal delivered to jailrun's OWN CLI process is
#            forwarded to the jexec'd subprocess's PID specifically — NOT to
#            the whole jail (unlike _stream_jexec's OWN timeout path, which
#            deliberately removes the whole jail — a different tradeoff
#            justified by that timeout's own contract; exec must not destroy
#            anything, the jail may have other work running in it, e.g. the
#            original `jailrun run` command)
#   input:
#     proc_holder: dict[str, Any] — mutable box that _exec_async's
#                  on_process_started callback populates with the live
#                  asyncio.subprocess.Process once _stream_jexec spawns it; if
#                  a signal arrives before the process exists yet, or after it
#                  has already exited (returncode is not None), forwarding is
#                  a no-op — nothing live to signal
#   output: a context manager (contextlib.contextmanager); installs handlers
#           on entry, restores whatever signal.getsignal() reported BEFORE
#           entry (which may itself be a caller's own prior customization, not
#           necessarily SIG_DFL) on exit — never leaks a global handler
#           override into the rest of the CLI process's lifetime
#   sideEffects: signal.signal(SIGINT, ...) / signal.signal(SIGTERM, ...) —
#                process-wide global state for the duration of the `with`
#                block; proc.send_signal() (== os.kill(pid, sig) under the
#                hood) when a forwarded signal actually fires
@contextlib.contextmanager
def _install_signal_forwarding(proc_holder: dict[str, Any]):
    """Forward SIGINT/SIGTERM to proc_holder['proc'] for the duration of the block."""

    def _make_handler(sig: int) -> Callable[[int, Any], None]:
        def _handler(_signum: int, _frame: Any) -> None:
            proc = proc_holder.get("proc")
            if proc is not None and proc.returncode is None:
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    # Already exited between the returncode check and the
                    # kill — nothing left to signal, not an error.
                    pass
        return _handler

    orig_sigint = signal.getsignal(signal.SIGINT)
    orig_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _make_handler(signal.SIGINT))
    signal.signal(signal.SIGTERM, _make_handler(signal.SIGTERM))
    try:
        yield
    finally:
        # Restore whatever was there before us, not necessarily SIG_DFL —
        # never leak a global handler override into the rest of the process.
        signal.signal(signal.SIGINT, orig_sigint)
        signal.signal(signal.SIGTERM, orig_sigterm)
# _install_signal_forwarding:end


# _exec_async:start
#   purpose: run cmd inside an ALREADY-RUNNING jail via _stream_jexec — the
#            async body behind exec_run(); creates and destroys nothing
#   input:
#     jail_name: str — name of an already-running jail (caller must have
#                already verified liveness — see _check_jail_live)
#     cmd: list[str] — command to run inside the jail (empty -> /bin/sh)
#     opts: dict[str, Any] — env (dict), workdir (str|None), timeout
#           (float|None, default DEFAULT_JEXEC_TIMEOUT_S)
#     proc_holder: dict[str, Any] — passed straight through to
#                  _stream_jexec's on_process_started hook so
#                  _install_signal_forwarding (installed by the caller,
#                  exec_run()) can find the live process to forward to
#   output:
#     exit_code: int — exact exit code from the jexec'd process
#   sideEffects: calls _stream_jexec(jail_name, cmd, conf_path=None, ...) —
#                conf_path=None because exec never wrote a jail.conf for this
#                jail (it did not create it, so it has no conf file to pass);
#                log_file=None (exec output is not persisted to `jailrun
#                logs` — that ledger belongs to the run that created the jail)
#   [KNOWN GAP — out of this task's scope] _stream_jexec's OWN `timeout`
#   parameter is reused as-is here (this task only adds SIGINT/SIGTERM
#   forwarding, per its explicit scope). If THIS opts["timeout"] (or the
#   DEFAULT_JEXEC_TIMEOUT_S default, 1800s) expires, _stream_jexec's existing
#   timeout path still runs `jail -r <jail_name>` and REMOVES THE WHOLE JAIL —
#   the same tradeoff _run_async's own timeout makes for `jailrun run`, which
#   is fine THERE because that jail belongs solely to that run. For `jailrun
#   exec`, this is arguably too broad (the jail may have other work in it,
#   e.g. the original `jailrun run` command) but is the SAME timeout
#   mechanism _stream_jexec always had, deliberately not changed by this
#   task — SIGINT/SIGTERM forwarding (the actual ask here) never triggers
#   this path at all, only an expired --timeout does. Flagged here rather
#   than silently accepted; a real fix would need _stream_jexec's timeout
#   path to distinguish "I created this jail" from "I did not" (e.g. via a
#   kill-whole-jail-on-timeout: bool param) — a separate, bigger change.
async def _exec_async(
    jail_name: str,
    cmd: list[str],
    opts: dict[str, Any],
    proc_holder: dict[str, Any],
) -> int:
    """Run cmd inside an existing jail via _stream_jexec. Creates/destroys nothing."""
    env: dict[str, str] = opts.get("env", {})
    workdir: str | None = opts.get("workdir")
    timeout: float | None = opts.get("timeout", DEFAULT_JEXEC_TIMEOUT_S)

    if not cmd:
        cmd = ["/bin/sh"]

    def _capture_proc(proc: Any) -> None:
        proc_holder["proc"] = proc

    return await _stream_jexec(
        jail_name,
        cmd,
        conf_path=None,
        env=env,
        workdir=workdir,
        timeout=timeout,
        log_file=None,
        on_process_started=_capture_proc,
    )
# _exec_async:end


# exec_run:start
#   purpose: synchronous public entry point for `jailrun exec` — bridges the
#            asyncio boundary for cli.py, exactly like run() does for
#            `jailrun run`, and installs/restores SIGINT/SIGTERM forwarding
#            around the whole call
#   input:
#     jail_name: str — name of an ALREADY-RUNNING jail (caller, cli.py's
#                _cmd_exec, must have already verified liveness via
#                _check_jail_live before calling this)
#     cmd: list[str] — command to run inside the jail (empty -> /bin/sh)
#     opts: dict[str, Any] — env (dict), workdir (str|None), timeout (float|None)
#   output:
#     exit_code: int — exact exit code returned by the command inside the jail
#   sideEffects: installs SIGINT/SIGTERM handlers for the duration of the call
#                (see _install_signal_forwarding — always restored, even on
#                exception, before this function returns) and drives
#                _exec_async(), which runs _stream_jexec() against jail_name;
#                does NOT create or destroy the jail or any ZFS clone — see
#                the module-level rationale above this section
def exec_run(jail_name: str, cmd: list[str], opts: dict[str, Any]) -> int:
    """
    Synchronous entry point called from cli.py's _cmd_exec.

    Runs `cmd` inside the ALREADY-RUNNING jail `jail_name` (docker-exec
    semantics — see the module-level rationale above this section). Unlike
    run(), this never creates or tears down a jail/clone: it only reuses
    _stream_jexec's jexec-invocation-and-streaming logic against an existing
    jail_name. SIGINT/SIGTERM received by this process while the command is
    running are forwarded to the jexec'd process specifically (not the whole
    jail — see _install_signal_forwarding).
    """
    proc_holder: dict[str, Any] = {}
    with _install_signal_forwarding(proc_holder):
        return asyncio.run(_exec_async(jail_name, cmd, opts, proc_holder))
# exec_run:end

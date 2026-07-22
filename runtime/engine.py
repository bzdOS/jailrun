# START_AI_HEADER
# MODULE: runtime/engine.py
# PURPOSE: S1 runtime core — hybrid native-first jail orchestrator
# INTENT: Coordinates S3/S2/S4 seams to unpack an image into a ZFS clone, build a
#         native-first PATH shadow layer, optionally load Linuxulator, start a
#         FreeBSD jail via jail(8), run the user command with jexec(8), stream I/O,
#         and tear down the jail and optionally destroy the clone on exit.
# DEPENDENCIES: stdlib (asyncio, json, logging, os, shlex, tempfile, textwrap, pathlib);
#               store (S3 seam — resolve/unpack/clone/mount/destroy ZFS datasets);
#               probe (S2 seam — produces substitution manifest for a rootfs);
#               bakery (S4 seam — fills native.artifact_path in the manifest);
#               runtime._mocks (fallback stubs when real seams are absent);
#               runtime.lifecycle (bsdos_lifecycled teardown, imported lazily);
#               runtime.rundb (RunDB run-state ledger, imported lazily — see the
#               record_start/record_exit call sites in _run_async);
#               FreeBSD system tools: jail(8), jexec(8), kldload(8), nullfs(5)
# PUBLIC_API: run(image_ref, cmd, opts) -> int
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
import json
import logging
import os
import shlex
import tempfile
import textwrap
from pathlib import Path
from typing import Any

log = logging.getLogger("jailrun.engine")


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


# _load_manifest:start
#   purpose: resolve the substitution manifest for image_ref, using the cached
#            file inside the rootfs clone or running probe+bakery on a cache miss
#   input:
#     rootfs_path: str — absolute path to the writable ZFS clone root
#     image_ref: str — image reference (used for logging and passed to probe)
#   output:
#     manifest: dict[str, Any] — parsed substitution manifest
#   sideEffects: on cache miss — calls _probe_module.probe(rootfs_path, image_ref)
#                and _bakery_module.bake(manifest); creates directory
#                <rootfs_path>/.jailrun/ and writes <rootfs_path>/.jailrun/substitution-manifest.json
def _load_manifest(rootfs_path: str, image_ref: str) -> dict[str, Any]:
    """
    Load the substitution manifest for *image_ref* from the rootfs clone.

    Probe + bakery are expected to have written the manifest into the rootfs at
    a well-known path, OR into the store's metadata area.  We check both.
    Convention: <rootfs_path>/.jailrun/<MANIFEST_FILENAME>

    Falls back to calling probe + bakery inline if the manifest is absent
    (first-run / no pre-bake).  This is the slow path.
    """
    # START_CHECK_MANIFEST_CACHE
    candidate = Path(rootfs_path) / ".jailrun" / MANIFEST_FILENAME
    if candidate.exists():
        log.debug("manifest found at %s", candidate)
        with candidate.open() as fh:
            return json.load(fh)
    # END_CHECK_MANIFEST_CACHE

    # START_PROBE_BAKE_SLOW_PATH
    log.info(
        "manifest not found for %s; running probe + bakery (slow path)", image_ref
    )
    manifest = _probe_module.probe(rootfs_path, image_ref)
    manifest = _bakery_module.bake(manifest)

    # Cache for next run.
    candidate.parent.mkdir(parents=True, exist_ok=True)
    with candidate.open("w") as fh:
        json.dump(manifest, fh, indent=2)

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
            log.warning(
                "binary %s: artifact_path=%s set but no bakery base is mounted "
                "for this run (no _bakery.snapshot_id) — cannot shadow, the "
                "substituted binary is not reachable inside this jail",
                entry.get("path"), artifact_path,
            )
            continue

        in_jail_path = base_prefix.rstrip("/") + "/" + artifact_path.lstrip("/")

        # Artifact-reality invariant (ARCHITECTURE.md): verify the resolved path
        # actually exists (through the mount) before ever shadowing it.
        if not (Path(rootfs_path) / in_jail_path.lstrip("/")).exists():
            log.warning(
                "binary %s: resolved native artifact %s does not exist inside "
                "the jail (bakery base missing this content?) — skipping shadow",
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
#   output:
#     conf_text: str — complete jail.conf snippet as a string (not written to disk here)
#   sideEffects: none
#   rationale: uses mount += per-mount (additive) to avoid the mount.fstab footgun
#              that would replace the entire fstab and silently break subsequent mounts
def _build_jail_conf(
    *,
    jail_name: str,
    rootfs_path: str,
    mounts: list[str],
    extra_params: list[str],
    linuxulator: bool,
    network: str = "none",
    allow_raw_sockets: bool = False,
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
    if network not in ("none", "inherit"):
        raise ValueError(f"network must be 'none' or 'inherit', got {network!r}")

    lines: list[str] = [
        f"{jail_name} {{",
        # rootfs_path is a Path (store.clone()'s real return type) — repr()'ing it
        # directly renders "PosixPath('/...')" into jail.conf, not a quoted string.
        # str() first so !r produces a normal quoted path.
        f"    path = {str(rootfs_path)!r};",
        "    persist;",                         # needed when we jexec rather than exec.start
        "    mount.devfs;",                     # /dev inside jail
    ]

    # [NESTED-JAIL devfs] When jailrun runs on the bare host, `mount.devfs`
    # defaults to devfs_ruleset 4 (devfsrules_jail), which correctly restricts
    # the run-jail's /dev to a safe minimal set (null/zero/random/...). But
    # applying a devfs ruleset is a HOST privilege: a process inside a jail
    # gets EPERM ("Operation not permitted") the moment jail(8) runs
    # `mount -t devfs -oruleset=4`, so nested jail creation fails outright.
    # Verified live on a nested production jail deployment 2026-07-22. When we
    # are ourselves jailed the only ruleset we may select is 0, so force it
    # explicitly.
    #
    # [SECURITY -- KNOWN GAP] ruleset 0 is the UNRESTRICTED devfs: a fresh
    # devfs mounted inside a jail exposes the FULL host device set (mem, kmem,
    # raw disks, ...), NOT the parent jail's already-restricted /dev — devfs
    # does not inherit the parent's ruleset. Jail policy still denies the
    # privileged operations on most of those nodes, but their mere presence is
    # a hardening gap that MUST be closed before running genuinely untrusted
    # code (e.g. user-uploaded C++ compiled through a nested jailrun). Closing
    # it requires host cooperation (host-mounted ruleset-4 devfs, or
    # recreating the parent jail with a child-capping devfs setup) and is
    # tracked as a separate gate.
    if _is_jailed():
        lines.append("    devfs_ruleset = 0;")   # only selectable ruleset when nested
        log.warning(
            "jailrun is running inside a jail: nested run-jail /dev uses the "
            "UNRESTRICTED devfs ruleset 0 (host devfs cannot delegate ruleset 4 "
            "from within a jail). Safe for trusted workloads; HARDEN before "
            "running untrusted code."
        )

    if allow_raw_sockets:
        lines.append("    allow.raw_sockets;")

    if network == "none":
        lines += ["    ip4 = disable;", "    ip6 = disable;"]
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
    "readbps:throttle=200m", # disk read throughput ceiling (rate — throttle, not kill)
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
#              lifecycled NotAvailable pattern) instead of spamming failures
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


# _stream_jexec:start
#   purpose: execute a command inside a running jail via jexec(8) and stream
#            its stdout/stderr line-by-line to the process's own stdout/stderr
#   input:
#     jail_name: str — name of an already-running jail (started with jail -c)
#     cmd: list[str] — command and arguments to run inside the jail
#     env: dict[str, str] — environment variables prepended via `env KEY=VALUE`
#     workdir: str | None — if set, cd to this path inside the jail before exec
#     timeout: float | None — seconds before killing the jailed process; None = no limit
#     conf_path: str | None — the jail.conf used to `-c` this jail; passed to an
#                emergency `jail -f <conf_path> -r` on timeout so mount += entries
#                get unmounted the same way normal teardown does (see rationale)
#   output:
#     exit_code: int — exact returncode of the process that ran inside the jail
#   sideEffects: spawns subprocess `jexec <jail_name> [sh -c 'cd <workdir> && ]
#                env KEY=VALUE ... <cmd>`; pipes jail process stdout line-by-line
#                to sys.stdout.buffer; pipes jail process stderr line-by-line to
#                sys.stderr.buffer; on timeout, removes the WHOLE JAIL (not just
#                the jexec'd process)
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
) -> int:
    """
    Execute `cmd` inside `jail_name` via jexec; stream stdout+stderr to our
    own stdout/stderr line-by-line.

    [GOTCHA] We use jexec, NOT jail exec.start, because exec.start returns 0
    on successful dispatch regardless of the command's own exit code.  jexec
    propagates the inner process returncode faithfully.

    env is passed via `jexec -l` (login environment) + explicit KEY=VALUE
    prepended to the command so they are seen by the target binary.

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

    async def _pipe_lines(stream: asyncio.StreamReader, sink: Any) -> None:
        # _pipe_lines: drains stream in raw chunks into sink.buffer (pure IO pump, no return value)
        # Chunk-based (stream.read), not line-based (readline): a build tool's
        # progress output (e.g. a `\r`-updated bar with no `\n` for a long
        # stretch) can exceed asyncio's default 64KB readline() buffer limit —
        # confirmed live 2026-07-19: a real esphome/platformio
        # compile crashed the whole run with "ValueError: Separator is not found,
        # and chunk exceed the limit". We only need passthrough streaming here,
        # not line boundaries, so chunked reads have no such limit at all.
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            sink.buffer.write(chunk)
            sink.buffer.flush()

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
#     - calls RunDB().record_start(jail_name, ...) right after `jail -c` succeeds
#       (best-effort — see runtime/rundb.py; never raises out of this function)
#     - runs jexec via _stream_jexec() — runs command inside the jail
#     - runs subprocess `jail -r <jail_name>` to remove the jail (always, in finally)
#     - calls runtime.lifecycle.teardown(jail_name) if lifecycled is present
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
    allow_raw_sockets: bool = opts.get("allow_raw_sockets", False)
    rctl_enabled: bool = opts.get("rctl_enabled", True)
    rctl_rules: tuple[str, ...] = (
        tuple(opts["rctl_rules"]) if opts.get("rctl_rules") else DEFAULT_RCTL_RULES
    ) if rctl_enabled else ()
    jexec_timeout: float | None = opts.get("timeout", DEFAULT_JEXEC_TIMEOUT_S)

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
    native_base: str | None = None
    if image_ref.startswith("nativebase:"):
        native_base = image_ref.split(":", 1)[1]

    if native_base:
        log.info("native FreeBSD base: %s", native_base)
        snapshot_id = _store_module.base_snapshot(native_base)
        log.info("snapshot: %s", snapshot_id)
    else:
        log.info("resolving image: %s", image_ref)
        image_id = _store_module.resolve(image_ref)
        log.info("image_id: %s", image_id)

        snapshot_id = _store_module.unpack(image_id)
        log.info("snapshot: %s", snapshot_id)
    # END_RESOLVE_AND_UNPACK

    # START_CLONE_ROOTFS
    # ------------------------------------------------------------------
    # 2. Clone → writable rootfs for this run
    # ------------------------------------------------------------------
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

    try:
        # START_LOAD_MANIFEST
        # ------------------------------------------------------------------
        # 3. Load substitution manifest (probe+bakery, or cache)
        # ------------------------------------------------------------------
        if native_base:
            # Native FreeBSD base: binaries are already native, so there is
            # nothing to probe or shadow and no Linux ABI to load. An empty
            # manifest makes every downstream step (bakery mount, shadow
            # symlinks, _needs_linuxulator) a correct no-op.
            manifest = {}
            log.info("native base %s: skipping manifest/shadow/Linuxulator", native_base)
        else:
            manifest = _load_manifest(rootfs_path, image_ref)
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
        jail_name = f"jailrun-{handle.id}"
        handle.jail_name = jail_name  # so store.destroy()'s own jail -r step is live too
        conf_text = _build_jail_conf(
            jail_name=jail_name,
            rootfs_path=rootfs_path,
            mounts=[],  # user -v volumes are mounted directly by store.mount() (step 4c)
            extra_params=[],
            linuxulator=linuxulator,
            network=network,
            allow_raw_sockets=allow_raw_sockets,
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

        exit_code = await _stream_jexec(
            jail_name,
            cmd,
            conf_path=conf_path,
            env=env,
            workdir=workdir,
            timeout=jexec_timeout,
        )
        log.info("jexec finished with exit code %d", exit_code)
        run_completed = True  # a real exit_code was produced — see record_exit below
        # END_JEXEC_RUN

    finally:
        # START_TEARDOWN
        # ------------------------------------------------------------------
        # 8. Teardown: stop jail; optionally destroy rootfs clone
        # ------------------------------------------------------------------
        if jail_name is not None:
            # Process teardown via bsdos_lifecycled (SIGKILL all PIDs + bsdOS
            # cleanup) when the daemon is present — best-effort, never fatal.
            # See runtime/lifecycle.py for the wire protocol + responsibility split.
            try:
                from runtime.lifecycle import teardown as _lc_teardown  # noqa: PLC0415
                await _lc_teardown(jail_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("lifecycled teardown of %s failed (non-fatal): %s", jail_name, exc)
            # Always remove the persist jail itself (works with or without lifecycled).
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

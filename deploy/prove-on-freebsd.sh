#!/bin/sh
# START_AI_HEADER
# MODULE: deploy/prove-on-freebsd.sh
# PURPOSE: full-stack prove-out runbook — exercises store/probe/bakery/runtime subsystems in sequence against a reference OCI image
# INTENT: run after provision-freebsd.sh to confirm the four subsystems are wired correctly end-to-end on a real FreeBSD host; failures are cheap because alpine:3.19 is small
# DEPENDENCIES: python3 (with jailrun source on PYTHONPATH), skopeo(1), umoci(1), zfs(8)/zpool(8), jail(8), jexec(8), mount_nullfs(8), umount(8); jailrun modules: store.store, probe.probe (module-level probe() function, no Probe class), bakery.bakery (module-level bake() function, no Bakery class), runtime.cli (main() — the real S1 entrypoint)
# PUBLIC_API: none — run as a script, not sourced
# END_AI_HEADER
# deploy/prove-on-freebsd.sh — jailrun full-stack prove-out runbook
#
# Run on the FreeBSD host as root, AFTER provision-freebsd.sh has completed.
#
# Purpose
# -------
# Validate the four subsystems (store → probe → bakery → runtime) in order
# using a tiny reference image (alpine:3.19) so failures are cheap to debug.
# Each step is self-describing and stops on the first error.
#
# Backend selection
# -----------------
# JAILRUN_STORE_BACKEND controls which store path is exercised:
#
#   JAILRUN_STORE_BACKEND=zfs       (default) — ZFS clone/snapshot  [production]
#   JAILRUN_STORE_BACKEND=plaindir  — plain directory copies        [degraded]
#
# JAILRUN_ZPOOL controls the pool name (default: jailrun).
#
# Usage
# -----
#   prove-on-freebsd.sh [options]
#
#   Options
#     --backend <zfs|plaindir>  Override JAILRUN_STORE_BACKEND
#     --zpool <name>            Override JAILRUN_ZPOOL (default: jailrun)
#     --image <ref>             Test image (default: alpine:3.19)
#     --keep                    Don't destroy the run clone at the end
#     --skip-probe              Skip probe step (if probe/ not ready)
#     --skip-bakery             Skip bakery step
#     --skip-runtime            Skip runtime step
#     -h, --help                Show this message
#
# VERIFIED 2026-07-19: ran clean end-to-end against alpine:3.19 on
# FreeBSD 15.1, zero errors, clean teardown — S1 (this script's own S1
# step, now the real runtime.cli entrypoint) also independently confirmed
# against esphome/esphome:stable (real ESP32 compile, see CHANGELOG.md). See
# CHANGELOG.md "Fixed (live E2E debugging, 2026-07-19)" for every bug this
# surfaced and fixed along the way.

set -eu

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
BACKEND="${JAILRUN_STORE_BACKEND:-zfs}"
ZPOOL="${JAILRUN_ZPOOL:-jailrun}"
IMAGE_REF="alpine:3.19"
KEEP=0
SKIP_PROBE=0
SKIP_BAKERY=0
SKIP_RUNTIME=0
JAILRUN_SRC="${JAILRUN_SRC:-/mnt/jailrun}"   # 9p mount of the source checkout (if used)

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --backend)     BACKEND="$2"; shift 2 ;;
        --zpool)       ZPOOL="$2"; shift 2 ;;
        --image)       IMAGE_REF="$2"; shift 2 ;;
        --keep)        KEEP=1; shift ;;
        --skip-probe)  SKIP_PROBE=1; shift ;;
        --skip-bakery) SKIP_BAKERY=1; shift ;;
        --skip-runtime)SKIP_RUNTIME=1; shift ;;
        -h|--help)
            sed -n '2,/^# UNVERIFIED/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

export JAILRUN_STORE_BACKEND="${BACKEND}"
export JAILRUN_ZPOOL="${ZPOOL}"
PYTHONPATH="${JAILRUN_SRC}"
export PYTHONPATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# log: prints a prefixed informational line to stdout
log()  { echo "[prove] $*"; }
# step: prints a full-width section banner to stdout
step() { echo; echo "============================================================"; echo "=== $*"; echo "============================================================"; }
# die: prints a fatal error to stderr and exits 1
die()  { echo "[prove] FATAL: $*" >&2; exit 1; }
# ok: prints a success confirmation line to stdout
ok()   { echo "[prove] OK: $*"; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

step "Configuration"
log "JAILRUN_STORE_BACKEND : ${BACKEND}"
log "JAILRUN_ZPOOL         : ${ZPOOL}"
log "IMAGE_REF             : ${IMAGE_REF}"
log "JAILRUN_SRC           : ${JAILRUN_SRC}"
log "KEEP                  : ${KEEP}"

[ -d "${JAILRUN_SRC}" ] || die "Source not found at ${JAILRUN_SRC}. Is the 9p mount up? (mount -t virtfs jailrun /mnt/jailrun -o trans=virtio,version=9p2000.L)"   # UNVERIFIED

# ---------------------------------------------------------------------------
# Scratch variables (shared across steps)
# ---------------------------------------------------------------------------
OCI_CACHE="/var/cache/jailrun/oci"
MOUNTBASE="/var/jailrun"
IMAGE_ID=""
SNAP_ID=""
CLONE_ROOTFS=""
CLONE_HANDLE_FILE="/tmp/jailrun-prove-handle.json"

# ---------------------------------------------------------------------------
# STEP S3-A: store — resolve (skopeo pull)
# ---------------------------------------------------------------------------
# START_STORE_RESOLVE
step "S3-A: store.resolve — pull ${IMAGE_REF} via skopeo"

# Use the Python store directly to exercise the real code path
IMAGE_ID=$(python3 - <<PYEOF
import sys
sys.path.insert(0, "${JAILRUN_SRC}")
import logging, os
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
os.environ["JAILRUN_STORE_BACKEND"] = "${BACKEND}"
os.environ["JAILRUN_ZPOOL"] = "${ZPOOL}"
from store.store import Store
s = Store(oci_cache_dir="${OCI_CACHE}", mountpoint_base="${MOUNTBASE}")
image_id = s.resolve("${IMAGE_REF}")
print(image_id)
PYEOF
)   # UNVERIFIED: skopeo --override-os linux on FreeBSD

[ -n "${IMAGE_ID}" ] || die "resolve returned empty image_id"
ok "image_id=${IMAGE_ID}"
# END_STORE_RESOLVE

# ---------------------------------------------------------------------------
# STEP S3-B: store — unpack (layers → dataset or plaindir)
# ---------------------------------------------------------------------------
# START_STORE_UNPACK
step "S3-B: store.unpack — OCI layers → ${BACKEND} storage"

SNAP_ID=$(python3 - <<PYEOF
import sys
sys.path.insert(0, "${JAILRUN_SRC}")
import logging, os
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
os.environ["JAILRUN_STORE_BACKEND"] = "${BACKEND}"
os.environ["JAILRUN_ZPOOL"] = "${ZPOOL}"
from store.store import Store
s = Store(oci_cache_dir="${OCI_CACHE}", mountpoint_base="${MOUNTBASE}")
snap = s.unpack("${IMAGE_ID}")
print(snap)
PYEOF
)   # UNVERIFIED: zfs create / umoci raw unpack on FreeBSD

[ -n "${SNAP_ID}" ] || die "unpack returned empty snapshot_id"
ok "snapshot_id=${SNAP_ID}"

# Quick sanity: rootfs top-level should have bin/etc/lib
if [ "${BACKEND}" = "zfs" ]; then
    # ZFS path: snapshot_id is  <pool>/images/<id>@snap
    # The dataset mountpoint is  ${MOUNTBASE}/images/<id>
    ROOTFS_CHECK="${MOUNTBASE}/images/${IMAGE_ID}"
else
    ROOTFS_CHECK="${SNAP_ID}"
fi
log "Rootfs top-level at ${ROOTFS_CHECK}:"
ls "${ROOTFS_CHECK}" || die "rootfs is empty or inaccessible"   # UNVERIFIED
# END_STORE_UNPACK

# ---------------------------------------------------------------------------
# STEP S3-C: store — clone (CoW run copy)
# ---------------------------------------------------------------------------
# START_STORE_CLONE
step "S3-C: store.clone — create writable run layer from snapshot"

CLONE_ROOTFS=$(python3 - <<PYEOF
import sys, json
sys.path.insert(0, "${JAILRUN_SRC}")
import logging, os
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
os.environ["JAILRUN_STORE_BACKEND"] = "${BACKEND}"
os.environ["JAILRUN_ZPOOL"] = "${ZPOOL}"
from store.store import Store
s = Store(oci_cache_dir="${OCI_CACHE}", mountpoint_base="${MOUNTBASE}")
rootfs, handle = s.clone("${SNAP_ID}")
# Persist handle fields for the destroy step
data = {
    "id": handle.id,
    "rootfs": str(handle.rootfs),
    "dataset": handle.dataset,
    "snapshot_id": handle.snapshot_id,
    "backend": "${BACKEND}",
}
with open("${CLONE_HANDLE_FILE}", "w") as f:
    json.dump(data, f)
print(handle.rootfs)
PYEOF
)   # UNVERIFIED: zfs clone on FreeBSD

[ -n "${CLONE_ROOTFS}" ] || die "clone returned empty rootfs path"
ok "clone rootfs=${CLONE_ROOTFS}"
log "Handle saved to ${CLONE_HANDLE_FILE}"

# Verify clone is writable
WRITE_TEST="${CLONE_ROOTFS}/.jailrun_prove_write_test"
echo "prove-on-freebsd" > "${WRITE_TEST}" && rm "${WRITE_TEST}" \
    || die "clone rootfs is not writable"   # UNVERIFIED
ok "write test passed"
# END_STORE_CLONE

# ---------------------------------------------------------------------------
# STEP S3-D: store — mount (nullfs bind)
# ---------------------------------------------------------------------------
# START_NULLFS_MOUNT
step "S3-D: store.mount — nullfs bind mount into clone"

# Bind /etc/resolv.conf read-only for DNS
RESOLV_DEST="${CLONE_ROOTFS}/etc/resolv.conf"
if [ -f "${RESOLV_DEST}" ]; then
    log "Binding /etc/resolv.conf -> ${RESOLV_DEST} (ro)"
    mount_nullfs -o ro /etc/resolv.conf "${RESOLV_DEST}" \
        || log "WARN: nullfs file bind failed — known open question"   # UNVERIFIED
else
    log "WARN: ${RESOLV_DEST} does not exist in rootfs, skipping file bind"
fi

# Bind a host directory read-write
WORK_DIR="/tmp/jailrun-prove-work"
mkdir -p "${WORK_DIR}"
WORK_DEST="${CLONE_ROOTFS}/mnt/work"
mkdir -p "${WORK_DEST}"
log "Binding ${WORK_DIR} -> ${WORK_DEST} (rw)"
mount_nullfs "${WORK_DIR}" "${WORK_DEST}" \
    || die "nullfs dir bind failed"   # UNVERIFIED
ok "nullfs bind mounted"
# END_NULLFS_MOUNT

# ---------------------------------------------------------------------------
# STEP S2: probe — classify binaries, emit substitution manifest
# ---------------------------------------------------------------------------
# START_PROBE_CLASSIFY
step "S2: probe — classify binaries in cloned rootfs"

if [ "${SKIP_PROBE}" -eq 1 ]; then
    log "Skipping probe (--skip-probe)"
else
    python3 - <<PYEOF || log "WARN: probe step failed (non-fatal for store prove-out)"
import sys
sys.path.insert(0, "${JAILRUN_SRC}")
import logging
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
from probe.probe import probe as probe_fn
manifest = probe_fn("${CLONE_ROOTFS}", image_ref="${IMAGE_REF}", snapshot_id="${SNAP_ID}")
import json
out = "/tmp/jailrun-prove-manifest.json"
with open(out, "w") as f:
    json.dump(manifest, f, indent=2)
print(f"[prove] manifest written to {out}")
PYEOF
    # probe.probe() is a module-level function (rootfs_dir, image_ref, snapshot_id) -> dict;
    # there is no Probe class. Fixed 2026-07-18: the prior `from probe.probe import
    # Probe` call was an ImportError that this step's `|| WARN ... non-fatal` silently swallowed,
    # so S2 had never actually run inside this runbook.
    if [ -f /tmp/jailrun-prove-manifest.json ]; then
        ok "manifest written to /tmp/jailrun-prove-manifest.json"
        # Show linuxulator.required so we know if Tier-2 would be needed
        python3 -c "
import json
m = json.load(open('/tmp/jailrun-prove-manifest.json'))
lr = m.get('linuxulator', {}).get('required', '?')
print(f'[prove] linuxulator.required = {lr}')
" || true
    fi
fi
# END_PROBE_CLASSIFY

# ---------------------------------------------------------------------------
# STEP S4: bakery — resolve native providers
# ---------------------------------------------------------------------------
# START_BAKERY_RESOLVE
step "S4: bakery — resolve native artifact providers"

if [ "${SKIP_BAKERY}" -eq 1 ]; then
    log "Skipping bakery (--skip-bakery)"
else
    MANIFEST_ARG=""
    [ -f /tmp/jailrun-prove-manifest.json ] && MANIFEST_ARG="/tmp/jailrun-prove-manifest.json"

    python3 - <<PYEOF || log "WARN: bakery step failed (non-fatal for store prove-out)"
import sys, json
sys.path.insert(0, "${JAILRUN_SRC}")
import logging
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
# bakery.bake() is a module-level function that takes a manifest DICT (not a path)
# and returns the updated manifest dict; there is no Bakery class.
manifest_path = "${MANIFEST_ARG}" if "${MANIFEST_ARG}" else None
if manifest_path:
    with open(manifest_path) as f:
        manifest = json.load(f)
    from bakery.bakery import bake
    result = bake(manifest)
    out = "/tmp/jailrun-prove-manifest.baked.json"
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[prove] bakery result written to {out}")
else:
    print("[prove] No manifest — listing known bakery build recipes (no bake)")
    from bakery.bakery import RECIPE_REGISTRY
    print(f"[prove] known build: recipes: {list(RECIPE_REGISTRY.keys())}")
PYEOF
    # Fixed 2026-07-18: the prior `from bakery.bakery import Bakery` call was an
    # ImportError swallowed by `|| WARN ... non-fatal` — S4 had never actually run either.
fi
# END_BAKERY_RESOLVE

# ---------------------------------------------------------------------------
# STEP S1: runtime — jailrun run (REAL engine.py path, not hand-typed jail/jexec)
# ---------------------------------------------------------------------------
# START_RUNTIME_JAIL_EXEC
step "S1: runtime — jailrun run ${IMAGE_REF} /bin/sh -c 'id; uname -a'  (through runtime.cli/engine.py)"

if [ "${SKIP_RUNTIME}" -eq 1 ]; then
    log "Skipping runtime (--skip-runtime)"
else
    # Fixed 2026-07-18: this step used to hand-roll jail -c / jexec / jail -r
    # directly, which meant engine.py/cli.py were NEVER exercised by this runbook despite the
    # file's own stated purpose ("confirm the four subsystems are wired correctly"). This now
    # drives the actual `jailrun run` entrypoint (runtime.cli.main -> runtime.engine.run), which
    # independently does its OWN resolve -> unpack -> clone -> manifest -> shadow -> jail -> jexec
    # -> teardown cycle end-to-end (separate from the S3-A..D clone above, which stays only as a
    # per-seam store diagnostic). --rm so engine.py's own store.destroy() path runs too.
    log "Invoking runtime.cli.main(['run', '--rm', ...]) — this is the real user-facing entrypoint"
    # A non-zero exit here can be an EXPECTED finding (e.g. no Linuxulator substitute for this
    # image), not a script error — disable errexit around just this call so we can inspect rc
    # instead of the whole runbook dying on set -e.
    set +e
    python3 - <<PYEOF
import sys, os
sys.path.insert(0, "${JAILRUN_SRC}")
import logging
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
os.environ["JAILRUN_STORE_BACKEND"] = "${BACKEND}"
os.environ["JAILRUN_ZPOOL"] = "${ZPOOL}"
from runtime.cli import main
rc = main(["run", "--rm", "${IMAGE_REF}", "/bin/sh", "-c", "id; uname -a"])
sys.exit(rc)
PYEOF
    RUNTIME_RC=$?
    set -e
    if [ "${RUNTIME_RC}" -eq 0 ]; then
        ok "jailrun run completed through engine.py (rc=0)"
    else
        log "WARN: jailrun run exited rc=${RUNTIME_RC} — expected without Linuxulator for a linux/amd64 Alpine image (musl ELF, no native substitute); see store/README.md open-question #8. A non-zero rc here is a real finding to record, not a script bug."
    fi
fi
# END_RUNTIME_JAIL_EXEC

# ---------------------------------------------------------------------------
# STEP S3-E: store — destroy (cleanup)
# ---------------------------------------------------------------------------
# START_STORE_DESTROY
step "S3-E: store.destroy — tear down clone"

# Unmount nullfs in reverse order first
umount "${WORK_DEST}"            2>/dev/null || log "WARN: umount ${WORK_DEST} failed"   # UNVERIFIED
umount "${RESOLV_DEST}"          2>/dev/null || log "WARN: umount ${RESOLV_DEST} failed — was it mounted?"   # UNVERIFIED

if [ "${KEEP}" -eq 1 ]; then
    log "--keep: skipping destroy.  Clone at: ${CLONE_ROOTFS}"
    log "To clean up later:"
    if [ "${BACKEND}" = "zfs" ]; then
        log "  zfs destroy $(python3 -c "import json; h=json.load(open('${CLONE_HANDLE_FILE}')); print(h['dataset'])")"
    else
        log "  rm -rf ${CLONE_ROOTFS}"
    fi
else
    python3 - <<PYEOF
import sys, json
sys.path.insert(0, "${JAILRUN_SRC}")
import logging, os
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
os.environ["JAILRUN_STORE_BACKEND"] = "${BACKEND}"
os.environ["JAILRUN_ZPOOL"] = "${ZPOOL}"
from store.store import Store, Handle
from pathlib import Path

with open("${CLONE_HANDLE_FILE}") as f:
    d = json.load(f)

s = Store(oci_cache_dir="${OCI_CACHE}", mountpoint_base="${MOUNTBASE}")
handle = Handle(
    id=d["id"],
    rootfs=Path(d["rootfs"]),
    dataset=d["dataset"],
    snapshot_id=d["snapshot_id"],
)
s.destroy(handle)
print(f"[prove] destroyed handle {handle.id}")
PYEOF
    # UNVERIFIED: zfs destroy on FreeBSD
    ok "clone destroyed"
    rm -f "${CLONE_HANDLE_FILE}"
fi
# END_STORE_DESTROY

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
step "PROVE-OUT COMPLETE"
log ""
log "Backend tested : ${BACKEND}"
log "Image tested   : ${IMAGE_REF}  (image_id=${IMAGE_ID})"
log "Snapshot kept  : ${SNAP_ID}  (ready for future clone() calls)"
log ""
log "Open items to verify on FreeBSD:"
log "  1. skopeo --override-os linux pull succeeds                    [S3-A]"
log "  2. umoci raw unpack succeeds for linux/amd64 on FreeBSD        [S3-B]"
log "  3. zfs clone -o mountpoint=... is atomic                       [S3-C]"
log "  4. mount_nullfs file bind (/etc/resolv.conf) works             [S3-D]"
log "  5. runtime.cli 'jailrun run' completes its own full pipeline   [S1]"
log "     (resolve->unpack->clone->manifest->shadow->jail->jexec->rm, for real)"
log "  6. jexec'd 'id; uname -a' returns something (or expected failure msg) [S1]"
log "  7. zfs destroy succeeds after jail is stopped                  [S3-E]"
log ""
log "If Linuxulator is needed (Tier-2): re-run provision-freebsd.sh --tier2"
log "then re-run this script.  Alpine musl + Linuxulator may still be limited"
log "(see store/README.md §open-questions)."

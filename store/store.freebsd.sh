#!/bin/sh
# START_AI_HEADER
# MODULE: store/store.freebsd.sh
# PURPOSE: end-to-end prove-out of the jailrun store lifecycle on FreeBSD
# INTENT: validates the full resolve->unpack->clone->mount->jail->destroy seam
#         using real ZFS, skopeo, umoci, and jail(8) before wiring up store.py;
#         each FreeBSD-specific step is tagged UNVERIFIED for host validation
# DEPENDENCIES: zfs(8), jail(8), jexec(8), skopeo, umoci, mount_nullfs(8), python3
# PUBLIC_API: none — standalone script, not sourced by other modules
# END_AI_HEADER
# store.freebsd.sh — jailrun S3 store prove-out script
#
# Run on freebsd-host (FreeBSD with ZFS + jail + skopeo + umoci installed).
# Demonstrates the full store lifecycle:
#   resolve -> unpack -> clone -> mount -> [run jail] -> destroy
# for a minimal image (alpine:3.19, linux/amd64).
#
# Every FreeBSD-specific step is marked  # UNVERIFIED
# and must be validated on freebsd-host.
#
# Prerequisites on freebsd-host:
#   pkg install -y skopeo umoci   (sysutils/skopeo sysutils/umoci)
#   ZFS pool named "zroot" with enough free space (~500 MB)
#   Run as root (zfs, jail, mount_nullfs require root)
#
# Usage:
#   sh store.freebsd.sh [pool]       (default pool: zroot)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POOL="${1:-zroot}"
STORE_DS="${POOL}/jailrun"
IMAGES_DS="${STORE_DS}/images"
RUNS_DS="${STORE_DS}/runs"
OCI_CACHE="/var/cache/jailrun/oci"
MOUNTBASE="/var/jailrun"
IMAGE_REF="alpine:3.19"
IMAGE_TAG="latest"
OCI_NAME="alpine_3.19"

# log: writes a labeled informational line to stderr (pure output, no IO side effects)
log()  { echo "[store.freebsd-host] $*" >&2; }
# die: writes a FATAL label to stderr and exits 1
die()  { echo "[store.freebsd-host] FATAL: $*" >&2; exit 1; }
# step: writes a section banner to stdout
step() { echo; echo "=== $* ==="; }

# START_BOOTSTRAP_ZFS
# Step 0: Bootstrap ZFS dataset hierarchy
# ---------------------------------------------------------------------------
step "0. Bootstrap ZFS dataset hierarchy"

for ds in "${STORE_DS}" "${IMAGES_DS}" "${RUNS_DS}"; do
    if ! zfs list "${ds}" >/dev/null 2>&1; then          # UNVERIFIED
        log "Creating dataset ${ds}"
        zfs create -p "${ds}"                             # UNVERIFIED
    else
        log "Dataset ${ds} already exists"
    fi
done

mkdir -p "${OCI_CACHE}" "${MOUNTBASE}/images" "${MOUNTBASE}/runs"
# END_BOOTSTRAP_ZFS

# START_RESOLVE_IMAGE
# Step 1: resolve — pull linux image with skopeo
# ---------------------------------------------------------------------------
step "1. resolve: skopeo copy docker://${IMAGE_REF} oci:..."

OCI_DIR="${OCI_CACHE}/${OCI_NAME}"
OCI_DEST="oci:${OCI_DIR}:${IMAGE_TAG}"

if [ -d "${OCI_DIR}/blobs" ]; then
    log "OCI layout already cached at ${OCI_DIR}, skipping pull"
else
    log "Pulling ${IMAGE_REF} -> ${OCI_DEST}"
    skopeo copy \
        --override-os linux \
        "docker://${IMAGE_REF}" \
        "${OCI_DEST}"                                     # UNVERIFIED: --override-os linux needed on FreeBSD
    log "Pull complete"
fi

# Compute image_id: sha256 of sorted layer digests
# (mirrors store.py _compute_image_id logic)
# Pass OCI_DIR as argv[1] so the single-quoted heredoc can receive it.
IMAGE_ID=$(python3 - "${OCI_DIR}" <<'PYEOF'
import hashlib, json, pathlib, sys
oci = pathlib.Path(sys.argv[1])
idx = json.loads((oci / "index.json").read_text())
mdig = idx["manifests"][0]["digest"]
alg, hex_ = mdig.split(":", 1)
mblob = oci / "blobs" / alg / hex_
manifest = json.loads(mblob.read_text())
digests = sorted(l["digest"] for l in manifest.get("layers", []))
image_id = hashlib.sha256("\n".join(digests).encode()).hexdigest()
print(image_id)
PYEOF
)

log "image_id = ${IMAGE_ID}"
# END_RESOLVE_IMAGE

# START_UNPACK_IMAGE
# Step 2: unpack — extract OCI layers into ZFS dataset, snapshot
# ---------------------------------------------------------------------------
step "2. unpack: OCI layers -> ZFS dataset -> snapshot"

IMAGE_DS="${IMAGES_DS}/${IMAGE_ID}"
IMAGE_SNAP="${IMAGE_DS}@snap"
IMAGE_MP="${MOUNTBASE}/images/${IMAGE_ID}"

if zfs list -t snapshot "${IMAGE_SNAP}" >/dev/null 2>&1; then   # UNVERIFIED
    log "Snapshot ${IMAGE_SNAP} already exists, skipping unpack"
else
    mkdir -p "${IMAGE_MP}"

    log "Creating ZFS dataset ${IMAGE_DS}"
    zfs create -o "mountpoint=${IMAGE_MP}" "${IMAGE_DS}"         # UNVERIFIED

    log "Unpacking layers with umoci"
    umoci raw unpack \
        --image "${OCI_DIR}:${IMAGE_TAG}" \
        "${IMAGE_MP}"                                            # UNVERIFIED: umoci must be installed

    log "Snapshotting ${IMAGE_SNAP}"
    zfs snapshot "${IMAGE_SNAP}"                                 # UNVERIFIED

    log "Marking dataset read-only"
    zfs set readonly=on "${IMAGE_DS}"                            # UNVERIFIED

    log "Unpack complete: $(du -sh "${IMAGE_MP}" | cut -f1) on disk"
fi

# Verify rootfs looks sane
log "Rootfs top-level:"
ls "${IMAGE_MP}" || true
# END_UNPACK_IMAGE

# START_CLONE_RUN
# Step 3: clone — CoW writable layer for a run
# ---------------------------------------------------------------------------
step "3. clone: zfs clone snapshot -> run dataset"

RUN_ID=$(python3 -c "import uuid; print(uuid.uuid4().hex)")
RUN_DS="${RUNS_DS}/${RUN_ID}"
RUN_MP="${MOUNTBASE}/runs/${RUN_ID}"

mkdir -p "${RUN_MP}"

log "Cloning ${IMAGE_SNAP} -> ${RUN_DS}"
zfs clone \
    -o "mountpoint=${RUN_MP}" \
    "${IMAGE_SNAP}" \
    "${RUN_DS}"                                                  # UNVERIFIED

log "Clone created: ${RUN_DS} mounted at ${RUN_MP}"
# END_CLONE_RUN

# START_MOUNT_NULLFS
# Step 4: mount — nullfs bind mounts
# ---------------------------------------------------------------------------
step "4. mount: mount_nullfs bind mounts"

# Bind /etc/resolv.conf read-only for DNS inside jail
RESOLV_DEST="${RUN_MP}/etc/resolv.conf"
if [ -f "${RESOLV_DEST}" ]; then
    log "Bind-mounting /etc/resolv.conf -> ${RESOLV_DEST} (ro)"
    mount_nullfs -o ro /etc/resolv.conf "${RESOLV_DEST}"         # UNVERIFIED: nullfs file bind on FreeBSD
else
    log "WARN: ${RESOLV_DEST} does not exist in rootfs, skipping resolv bind"
fi

# Bind /tmp as read-write example
TMP_DEST="${RUN_MP}/tmp"
mkdir -p "${TMP_DEST}"
log "Bind-mounting /tmp -> ${TMP_DEST} (rw)"
mount_nullfs /tmp "${TMP_DEST}"                                   # UNVERIFIED
# END_MOUNT_NULLFS

# START_JAIL_START
# Step 5: jail -c — start a minimal jail and run /bin/sh -c id
# ---------------------------------------------------------------------------
step "5. jail -c: start jail + run command"

JAIL_NAME="jailrun_${RUN_ID}"

log "Starting jail ${JAIL_NAME}"
# Note: linux jail (for an Alpine rootfs) requires linux64 ABI loaded:
#   kldload linux64   # UNVERIFIED
# For a quick smoke test with the native FreeBSD sh we skip linux ABI.
# A real jailrun run would set exec.system-jail-user + linux ABI if needed.

jail \
    -c \
    name="${JAIL_NAME}" \
    path="${RUN_MP}" \
    host.hostname="${JAIL_NAME}" \
    persist \
    allow.raw_sockets=false \
    mount.devfs=false \
    ip4=inherit                                                   # UNVERIFIED

log "Jail ${JAIL_NAME} started"

# Run a command inside — /bin/sh is Alpine's busybox sh, runs under Linuxulator
# For a plain "does it mount" smoke test without Linuxulator we check the path:
log "Files at /bin inside jail:"
jexec "${JAIL_NAME}" ls /bin || \
    log "WARN: ls /bin failed (expected if Linuxulator not loaded)"
# END_JAIL_START

# START_DESTROY_RUN
# Step 6: destroy — jail -r + umount + zfs destroy
# ---------------------------------------------------------------------------
step "6. destroy: jail -r + umount + zfs destroy"

log "Stopping jail ${JAIL_NAME}"
jail -r "${JAIL_NAME}" || log "WARN: jail -r failed (jail may not be running)"   # UNVERIFIED

log "Unmounting nullfs mounts (reverse order)"
umount "${TMP_DEST}"             2>/dev/null || log "WARN: umount ${TMP_DEST} failed"      # UNVERIFIED
umount "${RESOLV_DEST}"          2>/dev/null || log "WARN: umount ${RESOLV_DEST} failed"   # UNVERIFIED

log "Destroying ZFS clone ${RUN_DS}"
zfs destroy "${RUN_DS}"                                           # UNVERIFIED

rmdir "${RUN_MP}" 2>/dev/null || true
# END_DESTROY_RUN

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
step "DONE"
log "Lifecycle complete for image ${IMAGE_REF} (image_id=${IMAGE_ID})"
log "Snapshot ${IMAGE_SNAP} persists for future clone() calls."
log ""
log "Open questions for freebsd-host (see store/README.md):"
log "  1. Does 'umoci raw unpack' work on FreeBSD for linux/amd64 images?"
log "  2. Does mount_nullfs accept a file (not dir) as source for resolv.conf?"
log "  3. Does 'zfs set readonly=on' prevent writes from inside the jail?"
log "  4. Does ip4=inherit suffice or does the jail need explicit address assignment?"
log "  5. Is kldload linux64 needed before jailing an Alpine rootfs?"

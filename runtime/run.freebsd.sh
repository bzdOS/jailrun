#!/bin/sh
# START_AI_HEADER
# MODULE: runtime/run.freebsd.sh
# PURPOSE: end-to-end smoke test of the jailrun S1 run lifecycle on FreeBSD shell primitives
# INTENT: validates skopeo pull -> ZFS dataset -> umoci unpack -> snapshot -> clone ->
#         jail.conf write -> jail -c -> jexec -> jail -r -> zfs destroy without the
#         Python engine; finds FreeBSD-specific gotchas before wiring up engine.py
# DEPENDENCIES: skopeo, umoci, zfs(8), jail(8), jexec(8), kldload(8) for nullfs
# PUBLIC_API: none — standalone script; no arguments; all parameters hardcoded at top
# START_RATIONALE
# Q: why use jexec instead of exec.start in jail.conf?
# A: exec.start returns exit code 0 regardless of the inner command's exit code;
#    jexec propagates the exact inner returncode, which is required for smoke validation
# END_RATIONALE
# END_AI_HEADER
# UNVERIFIED — freebsd-host-only end-to-end smoke test for jailrun S1 runtime.
#
# Run this script on freebsd-host (FreeBSD, ZFS, Linuxulator available).
# It exercises the run lifecycle by hand for one image (alpine echo hi,
# plain jail — no Linuxulator) without using the Python engine at all.
# Purpose: validate the shell-level primitives that engine.py orchestrates,
# and find FreeBSD-specific gotchas before wiring up the full Python path.
#
# Prerequisites (install on freebsd-host):
#   pkg install -y skopeo umoci  # or oci-image-tool; for pull+unpack
#   zfs create zroot/jailrun     # pool for image/clone datasets
#   kldload nullfs               # usually already loaded
#
# Usage:
#   sh runtime/run.freebsd.sh
# All steps are idempotent; re-run freely.

set -eu
JAILRUN_POOL="zroot/jailrun"         # UNVERIFIED — adjust to your pool name
IMAGE="alpine:3.19"
IMAGE_SLUG="alpine-3.19"
JAIL_NAME="jailrun-smoke-$$"

# log: writes a colored informational line to stdout
log() { printf '\033[1;34m[jailrun-smoke]\033[0m %s\n' "$*"; }
# die: writes a colored FAIL label to stderr and exits 1
die() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# START_PULL_IMAGE
# Step 1: pull image with skopeo → OCI layout dir
# ---------------------------------------------------------------------------
log "Step 1: pull $IMAGE (skopeo)"
OCI_DIR="/tmp/jailrun-oci-${IMAGE_SLUG}"
if [ ! -d "$OCI_DIR" ]; then
    skopeo copy "docker://$IMAGE" "oci:${OCI_DIR}:latest" \
        || die "skopeo pull failed"                          # UNVERIFIED
fi
log "OCI layout: $OCI_DIR"
# END_PULL_IMAGE

# START_UNPACK_LAYERS
# Step 2: unpack OCI layers → ZFS snapshot ("image")
# ---------------------------------------------------------------------------
log "Step 2: unpack layers → ZFS dataset"
DATASET="${JAILRUN_POOL}/images/${IMAGE_SLUG}"
ROOTFS="/$(echo "$DATASET" | tr '/' '/')"  # zfs mount point

if ! zfs list "$DATASET" >/dev/null 2>&1; then
    zfs create -p "$DATASET"                                 # UNVERIFIED
    # umoci unpack into the new dataset.
    # Adjust tag ('latest') to match what skopeo wrote.
    umoci unpack --image "${OCI_DIR}:latest" --rootless \
        "$ROOTFS"                                            # UNVERIFIED
    # Remove umoci's config/manifest sidecar dirs (optional).
    zfs snapshot "${DATASET}@image"                          # UNVERIFIED
    log "snapshot: ${DATASET}@image"
else
    log "dataset already exists: $DATASET"
fi
# END_UNPACK_LAYERS

# START_CLONE_ROOTFS
# Step 3: clone snapshot → CoW writable rootfs for this run
# ---------------------------------------------------------------------------
log "Step 3: clone → writable rootfs"
RUN_DATASET="${JAILRUN_POOL}/runs/${JAIL_NAME}"
zfs clone "${DATASET}@image" "$RUN_DATASET"                 # UNVERIFIED
# zfs sets the mountpoint automatically; retrieve it.
ROOTFS_PATH="$(zfs get -H -o value mountpoint "$RUN_DATASET")"
log "rootfs: $ROOTFS_PATH"
# END_CLONE_ROOTFS

# START_WRITE_JAILCONF
# Step 4: write minimal jail.conf (plain jail — no Linuxulator for alpine echo)
# ---------------------------------------------------------------------------
log "Step 4: write jail.conf"
CONF_FILE="/tmp/${JAIL_NAME}.conf"
cat > "$CONF_FILE" <<JAILCONF
${JAIL_NAME} {
    path = "${ROOTFS_PATH}";
    persist;
    allow.raw_sockets;
    mount.devfs;
}
JAILCONF
# UNVERIFIED — mount.devfs requires devfs ruleset on freebsd-host; may need:
#   devfs_ruleset = "4";   (FreeBSD 13+)
cat "$CONF_FILE"
# END_WRITE_JAILCONF

# START_JAIL_RUN
# Step 5: start the jail
# ---------------------------------------------------------------------------
log "Step 5: jail -c"
jail -f "$CONF_FILE" -c "$JAIL_NAME"                        # UNVERIFIED
log "jail $JAIL_NAME created"
# END_JAIL_RUN

# START_JEXEC_CMD
# Step 6: jexec the command — use jexec NOT exec.start for exit code.
# [GOTCHA] jail exec.start returns 0 regardless of command exit code.
#          jexec propagates the exact inner returncode.
# ---------------------------------------------------------------------------
log "Step 6: jexec echo hi"
jexec "$JAIL_NAME" /bin/echo hi                             # UNVERIFIED
RC=$?
log "exit code: $RC"
[ "$RC" -eq 0 ] || die "expected exit code 0, got $RC"
# END_JEXEC_CMD

# START_DESTROY_JAIL
# Step 7: stop jail + destroy clone (--rm equivalent)
# ---------------------------------------------------------------------------
log "Step 7: jail -r + zfs destroy"
jail -r "$JAIL_NAME" || true                                # UNVERIFIED
zfs destroy "$RUN_DATASET"                                  # UNVERIFIED
rm -f "$CONF_FILE"
log "cleanup done"
# END_DESTROY_JAIL

log "SMOKE PASS: alpine echo hi in a plain jail — S1 primitives work"

# ---------------------------------------------------------------------------
# What to verify next (on freebsd-host, in order):
# ---------------------------------------------------------------------------
# 1. mount.devfs: confirm /dev is populated inside the jail (ls /dev).
# 2. nullfs bind: add a -v /host/path:/ctr/path bind and verify it's visible.
#    mount line: "mount += '/host/path ${ROOTFS_PATH}/ctr/path nullfs rw 0 0';"
#    [GOTCHA] nullfs uid/gid passthrough — host uid appears as-is inside jail.
# 3. Linuxulator: kldload linux64; mount linprocfs/linsysfs/fdescfs(linrdlnk);
#    jexec a Linux ELF binary and verify it runs.
# 4. Python engine: once store/ seam is built, run `jailrun run alpine echo hi`
#    and confirm the same flow goes through engine.py.

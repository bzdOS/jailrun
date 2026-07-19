#!/bin/sh
# START_AI_HEADER
# MODULE: deploy/provision-freebsd.sh
# PURPOSE: idempotent FreeBSD host provisioner — installs deps, creates ZFS pool and dataset hierarchy, scaffolds host dirs, optionally loads Linuxulator
# INTENT: one-shot setup script run as root on the target FreeBSD VM before any jailrun operation; safe to re-run because every step is guarded
# DEPENDENCIES: pkg(8), zpool(8), zfs(8), kldload(8), mkdir(1); external packages: skopeo umoci cmake ninja git
# PUBLIC_API: none — run as a script, not sourced
# END_AI_HEADER
# deploy/provision-freebsd.sh — idempotent FreeBSD host provisioner for jailrun
#
# Run on the FreeBSD host as root.  Safe to re-run: every step is guarded.
#
# What this script does
# ---------------------
#   1. pkg install: skopeo umoci cmake ninja git  (OCI/store dependencies)
#   2. Ensure the jailrun ZFS pool exists on a given device
#   3. Create the dataset hierarchy:  <zpool>/images  <zpool>/bases  <zpool>/runs
#   4. Create the OCI cache and mountpoint directories under /var
#
# Linuxulator / linux64
# ----------------------
# Native-first design: linux64.ko is NOT loaded here.
# Only load it if you need the Tier-2 OCI fallback (--tier2 flag below).
# Loading linux64 is not needed for native-substituted images (the common case).
#
# Usage
# -----
#   provision-freebsd.sh [options] [device]
#
#   device   Block device for the jailrun pool (e.g. /dev/vtbd1).
#             Also settable via JAILRUN_DISK.
#             If "existing", skip pool creation (pool must already exist).
#
#   Options
#     --zpool <name>   Pool name (default: jailrun; also JAILRUN_ZPOOL)
#     --tier2          Also load linux64/Linuxulator + linprocfs (Tier-2 OCI path)
#     --no-pkg         Skip pkg install (deps already present)
#     -h, --help       Show this message
#
# Examples
#   provision-freebsd.sh /dev/vtbd1            # create pool on vtbd1
#   provision-freebsd.sh existing              # pool already exists, just datasets
#   JAILRUN_ZPOOL=tank provision-freebsd.sh    # use a different pool name
#
# UNVERIFIED — this script has not been run on a real FreeBSD host yet.
# Mark findings when each step is confirmed.

set -eu

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
ZPOOL="${JAILRUN_ZPOOL:-jailrun}"
DEVICE="${JAILRUN_DISK:-}"
TIER2=0
DO_PKG=1

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --zpool)     ZPOOL="$2"; shift 2 ;;
        --tier2)     TIER2=1; shift ;;
        --no-pkg)    DO_PKG=0; shift ;;
        -h|--help)
            sed -n '2,/^# UNVERIFIED/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2; exit 1 ;;
        *)
            DEVICE="$1"; shift ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# log: prints a prefixed informational line to stdout
log()  { echo "[provision] $*"; }
# step: prints a section banner to stdout
step() { echo; echo "=== $* ==="; }
# die: prints a fatal error to stderr and exits 1
die()  { echo "[provision] FATAL: $*" >&2; exit 1; }

# CONTRACT: check effective uid -> call die (exit 1) if not root
require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root"
}

# ---------------------------------------------------------------------------
# Step 0: Sanity
# ---------------------------------------------------------------------------
# START_SANITY_CHECKS
require_root

step "0. Sanity checks"
log "ZPOOL=${ZPOOL}  DEVICE=${DEVICE:-<existing>}  TIER2=${TIER2}"

uname -r | grep -q FreeBSD || die "this script is for FreeBSD only"   # UNVERIFIED
# END_SANITY_CHECKS

# ---------------------------------------------------------------------------
# Step 1: pkg install dependencies
# ---------------------------------------------------------------------------
# START_PKG_INSTALL
step "1. Install pkg dependencies"

if [ "$DO_PKG" -eq 1 ]; then
    # Update pkg catalogue if older than 24 h                            # UNVERIFIED
    pkg update -q || log "WARN: pkg update failed (continuing)"

    # skopeo  — pull OCI images from registries (--override-os linux)
    # umoci   — unpack OCI layers correctly (whiteouts, xattrs)
    # cmake   — build deps for native toolchains (bakery)
    # ninja   — build system (bakery)
    # git     — source checkout (bakery recipes that build from source)
    PKGS="skopeo umoci cmake ninja git"
    log "Installing: ${PKGS}"
    pkg install -y ${PKGS}                                               # UNVERIFIED
    log "pkg install done"
else
    log "Skipping pkg install (--no-pkg)"
fi
# END_PKG_INSTALL

# ---------------------------------------------------------------------------
# Step 2: ZFS pool
# ---------------------------------------------------------------------------
# START_ZFS_POOL
step "2. Ensure ZFS pool '${ZPOOL}'"

if zpool list "${ZPOOL}" >/dev/null 2>&1; then                         # UNVERIFIED
    log "Pool '${ZPOOL}' already exists"
elif [ -z "${DEVICE}" ] || [ "${DEVICE}" = "existing" ]; then
    die "Pool '${ZPOOL}' does not exist and no device specified.
  Pass the device as an argument (e.g. /dev/vtbd1) or set JAILRUN_DISK."
else
    log "Creating pool '${ZPOOL}' on ${DEVICE}"
    # atime=off, compression=lz4 are sane defaults for a container store
    zpool create \
        -o ashift=12 \
        -O atime=off \
        -O compression=lz4 \
        -O mountpoint=none \
        "${ZPOOL}" \
        "${DEVICE}"                                                      # UNVERIFIED
    log "Pool created"
fi

# Verify pool is healthy                                                 # UNVERIFIED
POOL_STATE=$(zpool list -H -o health "${ZPOOL}" 2>/dev/null || echo "UNKNOWN")
log "Pool health: ${POOL_STATE}"
case "${POOL_STATE}" in
    ONLINE)  ;;
    *)       log "WARN: pool health is ${POOL_STATE} (expected ONLINE)" ;;
esac
# END_ZFS_POOL

# ---------------------------------------------------------------------------
# Step 3: Dataset hierarchy
# ---------------------------------------------------------------------------
# START_ZFS_DATASETS
step "3. Create dataset hierarchy under ${ZPOOL}/"

# Layout: <zpool>/images  <zpool>/bases  <zpool>/runs
# Mountpoints: /jailrun/images  /jailrun/bases  /jailrun/runs
# We use /jailrun as the base (dedicated disk — avoid filling the shared rootfs).
JAILRUN_MP="/jailrun"

for ds_suffix in images bases runs; do
    DS="${ZPOOL}/${ds_suffix}"
    MP="${JAILRUN_MP}/${ds_suffix}"
    if zfs list "${DS}" >/dev/null 2>&1; then                           # UNVERIFIED
        log "Dataset ${DS} already exists"
    else
        log "Creating dataset ${DS} -> ${MP}"
        zfs create -p -o "mountpoint=${MP}" "${DS}"                    # UNVERIFIED
    fi
done
# END_ZFS_DATASETS

# ---------------------------------------------------------------------------
# Step 4: Directory scaffolding for OCI cache and jailrun var
# ---------------------------------------------------------------------------
# START_HOST_DIRS
step "4. Host directories"

mkdir -p /var/cache/jailrun/oci
mkdir -p /var/jailrun/images /var/jailrun/bases /var/jailrun/runs

log "/var/cache/jailrun/oci — OCI pull cache (skopeo)"
log "/var/jailrun/{images,bases,runs} — legacy/override mountpoint_base"
# END_HOST_DIRS

# ---------------------------------------------------------------------------
# Step 5: Linuxulator (Tier-2 only)
# ---------------------------------------------------------------------------
# START_LINUXULATOR_SETUP
step "5. Linuxulator (Tier-2)"

if [ "${TIER2}" -eq 1 ]; then
    log "Loading linux64.ko and linprocfs (Tier-2 OCI fallback enabled)"
    kldload linux64  2>/dev/null || log "linux64 already loaded or failed"  # UNVERIFIED
    kldload linprocfs 2>/dev/null || log "linprocfs already loaded or failed"  # UNVERIFIED

    # Persist across reboots
    RCCONF="/etc/rc.conf"
    if ! grep -q 'linux_enable' "${RCCONF}" 2>/dev/null; then
        echo 'linux_enable="YES"' >> "${RCCONF}"                        # UNVERIFIED
        log "Added linux_enable=YES to ${RCCONF}"
    else
        log "linux_enable already set in ${RCCONF}"
    fi
else
    log "Skipping — native-first mode (linux64 NOT loaded)."
    log "Re-run with --tier2 to enable Linuxulator for the OCI fallback path."
fi
# END_LINUXULATOR_SETUP

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
step "DONE"
log ""
log "jailrun provisioning complete."
log "  Pool:     ${ZPOOL}"
log "  Datasets: ${ZPOOL}/images  ${ZPOOL}/bases  ${ZPOOL}/runs"
log "  OCI cache:/var/cache/jailrun/oci"
log ""
log "Next: run deploy/prove-on-freebsd.sh to validate the stack."
log ""
log "Environment for jailrun:"
log "  JAILRUN_ZPOOL=${ZPOOL}"
log "  JAILRUN_STORE_BACKEND=zfs"

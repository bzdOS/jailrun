#!/bin/sh
# START_AI_HEADER
# MODULE: bakery/bake.freebsd.sh
# PURPOSE: execute a bakery provisioning plan (pkg / port / source-build) on FreeBSD
# INTENT: consumes the _bakery.plan produced by bakery.py and drives actual
#         package/port/toolchain installation into a base jail dataset;
#         separates FreeBSD-native install mechanics from the Python planner
# DEPENDENCIES: pkg(8), ports tree at /usr/ports, jq(1), python3, git(1), gmake(1)
# PUBLIC_API: none — standalone script; entry point is argv[1]=manifest.json
# END_AI_HEADER
# UNVERIFIED — runs ONLY on freebsd-host (FreeBSD). Not executable on linux-host (Linux).
# bake.freebsd.sh — given a substitution manifest, execute its provisioning plan
# into a pkg/port-provisioned base jail/dataset.
#
# Usage:
#   bake.freebsd.sh <manifest.json> [<base-name>]
#
# The manifest is produced by probe (S2) and must already have _bakery.plan
# injected by bakery.py (run bakery.py first on linux-host, pass output here).
# If _bakery.plan is absent, this script reads native.provider fields directly.
#
# Requires on freebsd-host:
#   - pkg(8), ports tree at /usr/ports, jq(1), python3
#   - ZFS pool (zroot), jail(8)
#   - Root or sudo privileges

set -euo pipefail

# ---------------------------------------------------------------------------
MANIFEST="${1:?Usage: bake.freebsd.sh <manifest.json> [<base-name>]}"
BASE_NAME="${2:-jailrun-native-base}"
JAILROOT="/jails/${BASE_NAME}"
PORT_TREE="/usr/ports"
PKG_CONFIRM="-y"   # remove -y for interactive confirmation

# log: writes labeled line to stderr (pure output)
log() { printf '[bake] %s\n' "$*" >&2; }
# die: writes FATAL label to stderr and exits 1
die() { printf '[bake] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
[ "$(uname -s)" = "FreeBSD" ] || die "This script must run on FreeBSD (freebsd-host)."
command -v jq  >/dev/null 2>&1 || die "jq not found. pkg install jq"
command -v pkg >/dev/null 2>&1 || die "pkg not found."
[ -f "$MANIFEST" ] || die "Manifest not found: $MANIFEST"

# START_PARSE_PLAN
# Parse the provisioning plan
# If bakery.py has run, use _bakery.plan.steps. Otherwise fall back to
# reading native.provider fields directly from binaries[].
# ---------------------------------------------------------------------------
HAS_PLAN=$(jq 'has("_bakery")' "$MANIFEST")

if [ "$HAS_PLAN" = "true" ]; then
    log "Using _bakery.plan from manifest."
    STEPS=$(jq -c '._bakery.plan.steps[]' "$MANIFEST")
else
    log "No _bakery.plan — deriving steps from binaries[].native.provider."
    STEPS=$(jq -c '
        .binaries[]
        | select(.status == "native" and .native.provider != null)
        | {
            kind:  (.native.provider | split(":")[0]),
            target:(.native.provider | split(":")[1:] | join(":")),
            artifact_path: (.native.artifact_path // ""),
            notes: ""
          }
    ' "$MANIFEST")
fi
# END_PARSE_PLAN

# START_COLLECT_STEPS
# Collect unique pkg, port, build steps (maintaining order: pkg, port, build)
# ---------------------------------------------------------------------------
TMP=$(mktemp -d /tmp/bake.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

PKG_LIST="$TMP/pkgs.txt"
PORT_LIST="$TMP/ports.txt"
BUILD_LIST="$TMP/builds.txt"
: > "$PKG_LIST"; : > "$PORT_LIST"; : > "$BUILD_LIST"

echo "$STEPS" | while IFS= read -r step; do
    KIND=$(echo "$step" | jq -r '.kind')
    TARGET=$(echo "$step" | jq -r '.target')
    case "$KIND" in
        pkg)   grep -qxF "$TARGET" "$PKG_LIST"   || echo "$TARGET"   >> "$PKG_LIST"   ;;
        port)  grep -qxF "$TARGET" "$PORT_LIST"  || echo "$TARGET"   >> "$PORT_LIST"  ;;
        build) grep -qxF "$TARGET" "$BUILD_LIST" || echo "$TARGET"   >> "$BUILD_LIST" ;;
        *)     log "WARNING: unknown step kind '$KIND', skipping." ;;
    esac
done
# END_COLLECT_STEPS

# START_PKG_INSTALL
# Step 1 — pkg install
# ---------------------------------------------------------------------------
if [ -s "$PKG_LIST" ]; then
    PKG_NAMES=$(tr '\n' ' ' < "$PKG_LIST")
    log "pkg install $PKG_CONFIRM -- $PKG_NAMES"
    # shellcheck disable=SC2086
    pkg install $PKG_CONFIRM $PKG_NAMES
else
    log "No pkg steps."
fi
# END_PKG_INSTALL

# START_PORT_BUILDS
# Step 2 — port builds
# ---------------------------------------------------------------------------
if [ -s "$PORT_LIST" ]; then
    log "Building ports from $PORT_TREE ..."
    while IFS= read -r origin; do
        PORT_DIR="${PORT_TREE}/${origin}"
        if [ ! -d "$PORT_DIR" ]; then
            die "Port directory not found: $PORT_DIR — is ports tree up to date? (portsnap fetch update)"
        fi
        log "  Building port: $origin"
        # UNVERIFIED: options choices may need interactive selection first.
        (cd "$PORT_DIR" && make $PKG_CONFIRM install clean BATCH=yes)
    done < "$PORT_LIST"
else
    log "No port steps."
fi
# END_PORT_BUILDS

# START_SOURCE_BUILDS
# Step 3 — source builds (recipes)
# ---------------------------------------------------------------------------
if [ -s "$BUILD_LIST" ]; then
    log "Source builds ..."
    while IFS= read -r recipe_id; do
        case "$recipe_id" in

            xtensa-lx106-elf)
                # ESP8266 toolchain — PRIMARY: devel/esp-quick-toolchain port
                # (trombik unofficial port, GCC 10).
                # FALLBACK: jcmvbkbc/crosstool-NG lx106 branch.
                log "  build:xtensa-lx106-elf — trying esp-quick-toolchain port first."
                ESP_QUICK_PORT="${PORT_TREE}/devel/esp-quick-toolchain"
                if [ -d "$ESP_QUICK_PORT" ]; then
                    log "  Found devel/esp-quick-toolchain — building."
                    # UNVERIFIED
                    (cd "$ESP_QUICK_PORT" && make $PKG_CONFIRM install clean BATCH=yes)
                else
                    log "  devel/esp-quick-toolchain not in tree — falling back to crosstool-NG."
                    _build_xtensa_lx106_crosstool
                fi
                ;;

            riscv32-esp-elf)
                # STUB — no native FreeBSD port exists (2026-06).
                # Recommended: keep as linuxulator in manifest.
                log "  WARNING: build:riscv32-esp-elf is a stub. No native FreeBSD"
                log "           toolchain available. Set status=linuxulator in manifest"
                log "           and run via Linuxulator instead."
                ;;

            *)
                log "  WARNING: Unknown recipe '$recipe_id' — no handler. Skipping."
                ;;
        esac
    done < "$BUILD_LIST"
else
    log "No source build steps."
fi
# END_SOURCE_BUILDS

# ---------------------------------------------------------------------------
# Crosstool-NG fallback for xtensa-lx106-elf
# UNVERIFIED — FreeBSD ct-ng support is experimental per upstream docs.
# ---------------------------------------------------------------------------
# _build_xtensa_lx106_crosstool:start
#   purpose: build ESP8266 xtensa-lx106-elf cross-compiler from jcmvbkbc/crosstool-NG
#            when devel/esp-quick-toolchain is absent from the ports tree
#   input:
#     TMP: path — temp directory created by mktemp; used for clone and build artifacts
#   output:
#     none (void) — toolchain installed to /usr/local/xtensa-lx106-elf on success
#   sideEffects:
#     runs 'pkg install' for ~14 build prerequisites;
#     runs 'git clone --depth 1 --branch lx106 https://github.com/jcmvbkbc/crosstool-NG.git';
#     runs './bootstrap', './configure', 'gmake', 'gmake install' inside CT_BUILD_DIR;
#     runs 'ct-ng xtensa-lx106-elf' and 'ct-ng build' to compile the toolchain;
#     writes CT_PREFIX_DIR override line to $CT_BUILD_DIR/.config;
#     installs toolchain binaries under /usr/local/xtensa-lx106-elf/bin/
#   rationale: devel/esp-quick-toolchain is an unofficial trombik port; if absent,
#              jcmvbkbc/crosstool-NG lx106 branch is the only known working source
_build_xtensa_lx106_crosstool() {
    log "  Installing crosstool-NG FreeBSD prerequisites ..."
    # From: https://crosstool-ng.github.io/docs/os-setup/
    pkg install $PKG_CONFIRM \
        archivers/zip devel/automake devel/bison devel/gettext-tools \
        devel/git devel/gmake devel/gperf devel/libatomic_ops \
        devel/libtool devel/patch lang/gawk misc/help2man \
        print/texinfo textproc/asciidoc textproc/gsed textproc/xmlto

    CT_BUILD_DIR="${TMP}/crosstool-ng"
    CT_PREFIX="/usr/local/xtensa-lx106-elf"

    log "  Cloning jcmvbkbc/crosstool-NG (lx106 branch) ..."
    # UNVERIFIED
    git clone --depth 1 --branch lx106 \
        https://github.com/jcmvbkbc/crosstool-NG.git "$CT_BUILD_DIR"

    log "  Bootstrapping crosstool-NG ..."
    (
        cd "$CT_BUILD_DIR"
        ./bootstrap
        # FreeBSD: must point to GNU tools explicitly
        ./configure \
            --prefix="$CT_BUILD_DIR/inst" \
            --with-sed=/usr/local/bin/gsed \
            --with-make=/usr/local/bin/gmake \
            --with-patch=/usr/local/bin/gpatch
        gmake
        gmake install

        # Configure for xtensa-lx106-elf target
        export PATH="$CT_BUILD_DIR/inst/bin:$PATH"
        ct-ng xtensa-lx106-elf

        # Override install prefix
        printf '\nCT_PREFIX_DIR="%s"\n' "$CT_PREFIX" >> .config

        # UNVERIFIED: build may fail on FreeBSD due to experimental ct-ng support.
        ct-ng build
    )

    log "  Toolchain installed to $CT_PREFIX"
    log "  Binaries: $CT_PREFIX/bin/xtensa-lx106-elf-gcc et al."
}
# _build_xtensa_lx106_crosstool:end

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
log "Provisioning complete."
log "Base name: $BASE_NAME"
log ""
log "Next: run bakery.py to register this base with S3:"
log "  python3 bakery/bakery.py <manifest.json> | store register-base"
log ""
log "Or pass the plan JSON to store.register_base() directly."

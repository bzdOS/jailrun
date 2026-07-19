#!/bin/sh
# START_AI_HEADER
# MODULE: probe/smoke.freebsd.sh
# PURPOSE: run Linux binaries under a Linuxulator jail and harvest ENOSYS syscall gaps
# INTENT: S2 probe step — exercises each linuxulator-status binary from the manifest
#         via truss(1)+ktrace(1), extracts ENOSYS returns, and emits a JSON-Lines patch
#         for linuxulator.gaps back into the manifest; drives the native-vs-linuxulator
#         decision in bakery.py
# DEPENDENCIES: jail(8), jexec(8), truss(1), ktrace(1), kdump(1), kldstat(8), python3,
#               linux64 kmod, linux_base-rl9 pkg (CentOS/RHEL 9 Linux userland)
# PUBLIC_API: none — standalone script; argv: <rootfs_dir> <manifest.json> [binary ...]
# END_AI_HEADER
# smoke.freebsd.sh — jailrun S2: Linuxulator smoke test
# UNVERIFIED — runs on freebsd-host (FreeBSD) only; linux-host (Linux) cannot execute this.
#
# PURPOSE
#   For each Linux binary that the manifest marks status=linuxulator, run it
#   incrementally under truss(1) and/or ktrace(1) inside a Linuxulator-enabled
#   jail.  Harvest ENOSYS returns → feed back into the manifest's
#   syscalls_needed and linuxulator.gaps arrays.
#
# PREREQUISITES (freebsd-host)
#   - kldload linux64          (Linuxulator kernel module)
#   - pkg install linux_base-rl9   (CentOS/RHEL 9 Linux userland)
#   - /compat/linux mounted (linprocfs, linsysfs, tmpfs) per rc.conf:
#       linux_enable="YES"
#   - Caller passes ROOTFS pointing to an unpacked OCI image clone
#     (a ZFS dataset cloned from the snapshot — mutable layer).
#   - The binary list comes from the manifest JSON:
#       python3 -c "import json,sys; [print(b['path']) for b in
#         json.load(open(sys.argv[1]))['binaries']
#         if b['status']=='linuxulator']" MANIFEST
#
# USAGE
#   smoke.freebsd.sh <rootfs_dir> <manifest.json> [<binary> ...]
#
#   If no binaries are named, the script reads the linuxulator ones from the
#   manifest.  Results are merged back into the manifest's gaps array and
#   printed to stdout as a JSON patch (array of {path, syscalls}).
#
# HOW IT WORKS
#   1. Create a minimal FreeBSD jail that has /compat/linux bind-mounted and
#      the rootfs visible at /.
#   2. For each binary, run `truss -f <binary> --help 2>&1 | grep ENOSYS`
#      to exercise the startup path.  --help / -V are the cheapest safe flags.
#   3. Also run ktrace -t C <binary> --help; kdump | grep -i 'nosys\|ENOSYS'
#      for kernel-level confirmation.
#   4. Parse the ENOSYS lines to extract syscall names.
#   5. Emit JSON: { "binary": <path>, "enosys_calls": [...] }
#      one record per binary, one per line (JSON-Lines).
#
# KNOWN LINUXULATOR GAPS (2024-2026, for risk annotation)
#   - signalfd(2)       Bug 285881 — still unimplemented as of FreeBSD 14.x
#   - inotify_init(2)   Bug 240874 — unimplemented (kqueue does not map cleanly)
#   - inotify_init1(2)  same as above
#   - io_uring_setup(2) Not implemented — heavy kernel machinery, no upstream plan
#   - io_uring_enter(2) same as above
#   - io_uring_register(2) same as above
#   - fanotify_init(2)  Not implemented
#   - userfaultfd(2)    Not implemented
#   - pidfd_open(2)     Partial / unverified
#   - memfd_create(2)   Partial
#   - clone3(2)         Partial (old clone(2) works for basic thread creation)
#
# REFERENCES
#   truss(1) man.freebsd.org/cgi/man.cgi?query=truss
#   ktrace(1) man.freebsd.org/cgi/man.cgi?query=ktrace
#   FreeBSD Linuxulator wiki: wiki.freebsd.org/Linuxulator
#   Bug 285881 (signalfd): bugs.freebsd.org/bugzilla/show_bug.cgi?id=285881
#   Bug 240874 (inotify):  bugs.freebsd.org/bugzilla/show_bug.cgi?id=240874

set -eu

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
if [ $# -lt 2 ]; then
    echo "Usage: $0 <rootfs_dir> <manifest.json> [binary ...]" >&2
    exit 1
fi

ROOTFS="$1"; shift
MANIFEST="$1"; shift

if [ ! -d "$ROOTFS" ]; then
    echo "ERROR: rootfs not found: $ROOTFS" >&2
    exit 1
fi
if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: manifest not found: $MANIFEST" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Collect binaries to smoke
# ---------------------------------------------------------------------------
if [ $# -gt 0 ]; then
    BINARIES="$*"
else
    # Extract linuxulator binaries from manifest
    # UNVERIFIED: requires python3 on freebsd-host (pkg install python3 or already present)
    BINARIES="$(python3 -c "
import json, sys
m = json.load(open('$MANIFEST'))
for b in m['binaries']:
    if b.get('status') == 'linuxulator':
        print(b['path'].lstrip('/'))
")"
fi

if [ -z "$BINARIES" ]; then
    echo '{"info": "no linuxulator binaries found in manifest; smoke not needed"}' >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# Verify Linuxulator is loaded
# ---------------------------------------------------------------------------
if ! kldstat -n linux64 >/dev/null 2>&1; then
    echo "ERROR: linux64 kernel module not loaded. Run: kldload linux64" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Jail parameters — create a transient jail around the rootfs
# ---------------------------------------------------------------------------
JAIL_NAME="jailrun_smoke_$$"
KTRACE_FILE="/tmp/smoke_ktrace_$$.out"
ENOSYS_LOG="/tmp/smoke_enosys_$$.jsonl"

# cleanup:start
#   purpose: tear down transient smoke jail and remove ktrace temp files on exit
#   input:
#     JAIL_NAME: str — jail name set at script scope
#     KTRACE_FILE: path — ktrace output file set at script scope
#   output:
#     none
#   sideEffects:
#     runs 'jail -r $JAIL_NAME' to stop the jail;
#     removes $KTRACE_FILE and ${KTRACE_FILE}.raw temp files
cleanup() {
    # Tear down the jail if it exists
    jail -r "$JAIL_NAME" 2>/dev/null || true
    rm -f "$KTRACE_FILE" "${KTRACE_FILE}.raw" 2>/dev/null || true
}
# cleanup:end
trap cleanup EXIT INT TERM

# START_JAIL_CREATE
echo "# jailrun smoke: starting jail $JAIL_NAME over $ROOTFS" >&2

# UNVERIFIED: jail(8) parameter syntax; adjust for host FreeBSD version.
jail -c \
    name="$JAIL_NAME" \
    path="$ROOTFS" \
    host.hostname="smoke-probe" \
    persist \
    allow.mount \
    allow.mount.linprocfs \
    allow.mount.linsysfs \
    allow.mount.tmpfs \
    linux=1 \
    command=/bin/true 2>&1 || {
        echo "ERROR: jail creation failed — check FreeBSD version and permissions." >&2
        exit 1
    }

# Mount Linuxulator pseudo-filesystems inside the jail
# (idempotent: ignore errors if already mounted)
jexec "$JAIL_NAME" mount -t linprocfs  linprocfs  /proc    2>/dev/null || true
jexec "$JAIL_NAME" mount -t linsysfs   linsysfs   /sys     2>/dev/null || true
jexec "$JAIL_NAME" mount -t tmpfs      tmpfs       /dev/shm 2>/dev/null || true
# END_JAIL_CREATE

# START_SMOKE_BINARIES
# Smoke each binary
# ---------------------------------------------------------------------------
> "$ENOSYS_LOG"   # truncate output log

for rel_bin in $BINARIES; do
    ABS_BIN="/$rel_bin"

    echo "# smoking $ABS_BIN" >&2

    # ---- truss pass ----
    # truss -f forks; -o sends trace to file; we grep for ENOSYS
    TRUSS_OUT="$(
        jexec "$JAIL_NAME" \
            truss -f -o /dev/stderr \
            "$ABS_BIN" --help 2>&1 || true
    )"

    # Extract syscall names from truss ENOSYS lines.
    # truss format: "  <pid>  <syscall>(<args>)  ERR#78 'Function not implemented'"
    # ERR#78 = ENOSYS on Linux (syscall number is platform-specific; truss decodes it)
    TRUSS_ENOSYS="$(echo "$TRUSS_OUT" \
        | grep -E "ENOSYS|Function not implemented|ERR#78" \
        | sed -E "s/^[[:space:]]*[0-9]+[[:space:]]+([a-z_0-9]+)\(.*/\1/" \
        | sort -u || true)"

    # ---- ktrace pass ----
    # ktrace -t C traces system calls; kdump decodes them.
    rm -f "$KTRACE_FILE" 2>/dev/null || true
    jexec "$JAIL_NAME" \
        ktrace -t C -f "$KTRACE_FILE" \
        "$ABS_BIN" --help >/dev/null 2>&1 || true

    KTRACE_ENOSYS=""
    if [ -f "$KTRACE_FILE" ]; then
        KTRACE_ENOSYS="$(
            kdump -f "$KTRACE_FILE" 2>/dev/null \
                | grep -iE "nosys|ENOSYS" \
                | sed -E "s/.*linux_([a-z_0-9]+).*/\1/" \
                | sort -u || true
        )"
    fi

    # ---- merge results ----
    ALL_ENOSYS="$(printf '%s\n%s\n' "$TRUSS_ENOSYS" "$KTRACE_ENOSYS" \
        | grep -v '^$' | sort -u | tr '\n' ',' | sed 's/,$//')"

    # Emit JSON-Lines record
    python3 -c "
import json, sys
calls = [c for c in '$ALL_ENOSYS'.split(',') if c]
rec = {
    'binary':       '/$rel_bin',
    'enosys_calls': calls,
    'note':         'captured by truss+ktrace under Linuxulator jail'
}
print(json.dumps(rec))
" >> "$ENOSYS_LOG"

    echo "  $ABS_BIN: enosys=[${ALL_ENOSYS:-none}]" >&2
done
# END_SMOKE_BINARIES

# START_EMIT_RESULTS
# Emit the final patch on stdout (JSON-Lines → one object per binary)
# ---------------------------------------------------------------------------
echo "# ENOSYS results (JSON-Lines, one record per binary):" >&2
cat "$ENOSYS_LOG"
# END_EMIT_RESULTS

# ---------------------------------------------------------------------------
# Usage note: merge back into manifest
# ---------------------------------------------------------------------------
# After running this script, merge the ENOSYS data back into the manifest with:
#
#   python3 - <<'PY'
#   import json
#   manifest = json.load(open("manifest.json"))
#   patches  = [json.loads(l) for l in open("smoke_enosys.jsonl") if l.strip()]
#   patch_by_path = {p['binary']: p['enosys_calls'] for p in patches}
#   all_gaps = set()
#   for b in manifest['binaries']:
#       calls = patch_by_path.get(b['path'], [])
#       b['syscalls_needed'] = calls
#       all_gaps.update(calls)
#   manifest['linuxulator']['gaps'] = sorted(all_gaps)
#   json.dump(manifest, open("manifest.json", "w"), indent=2)
#   PY
#
# UNVERIFIED: jail parameters, mount commands, and truss output format may
# vary between FreeBSD 13.x and 14.x.  Adjust JAIL_NAME persistence and
# mount flags as needed for the target host.

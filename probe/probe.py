# START_AI_HEADER
# MODULE: probe/probe.py
# PURPOSE: ELF-OSABI binary classifier that walks a rootfs and emits a Substitution Manifest
# INTENT: Stage 2 of jailrun compat-intelligence pipeline — before a Linux container image
#         can run in a FreeBSD jail the operator needs to know which binaries are Linux ELF
#         (linuxulator candidates), which are already FreeBSD ELF (native, no shim needed),
#         and which have a pkg/port drop-in so linuxulator can be avoided entirely.
#         This module answers that question as a JSON manifest consumed by the bakery stage.
# DEPENDENCIES: stdlib only (argparse, json, os, struct, sys, datetime, pathlib);
#               providers (PROVIDER_MAP, loaded from providers/provider-map.json);
#               no pkg/ports/git; ELF parsing is hand-rolled byte reads (elf(5)).
# PUBLIC_API: probe(rootfs_dir, image_ref, snapshot_id) -> dict
#             main() -> None (CLI entry point)
# END_AI_HEADER
#!/usr/bin/env python3
"""
probe.py — jailrun S2: compat intelligence
Given an unpacked rootfs directory, walks executables, classifies each by ELF
OSABI / shebang, assigns role + status, proposes native.provider candidates,
and emits a Substitution Manifest conforming to
schemas/substitution-manifest.schema.json.

Usage:
    python3 probe.py <rootfs_dir> [--image IMAGE_REF] [--out manifest.json]

Pure Python; no pkg/ports/git. ELF parsing is a hand-rolled byte read.
Linuxulator smoke is scripted separately in smoke.freebsd.sh (freebsd-host-only).
"""

import argparse
import json
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

from providers import PROVIDER_MAP  # noqa: F401 — re-exported below for probe.probe.PROVIDER_MAP callers

# ---------------------------------------------------------------------------
# ELF constants (SYSV ABI + elf(5) — man7.org/linux/man-pages/man5/elf.5.html)
# ---------------------------------------------------------------------------
EI_MAG0, EI_MAG1, EI_MAG2, EI_MAG3 = 0, 1, 2, 3
EI_CLASS   = 4   # 1=32-bit, 2=64-bit
EI_DATA    = 5   # 1=little-endian, 2=big-endian
EI_OSABI   = 7   # OS/ABI identification byte

ELFMAG = b"\x7fELF"

# EI_OSABI values (System V ABI + OS-specific supplements)
ELFOSABI_NONE    = 0   # System V / unspecified → treated as Linux by convention
ELFOSABI_HPUX    = 1
ELFOSABI_NETBSD  = 2
ELFOSABI_GNU     = 3   # GNU / Linux (ELFOSABI_LINUX is an alias for this)
ELFOSABI_LINUX   = 3   # alias
ELFOSABI_SOLARIS = 6
ELFOSABI_AIX     = 7
ELFOSABI_IRIX    = 8
ELFOSABI_FREEBSD = 9
ELFOSABI_TRU64   = 10
ELFOSABI_OPENBSD = 12
ELFOSABI_ARM_ABI = 64
ELFOSABI_ARM     = 97
ELFOSABI_STANDALONE = 255

# e_machine values (a subset; ELF spec table 4)
EM_NONE    = 0
EM_386     = 3
EM_PPC     = 20
EM_PPC64   = 21
EM_ARM     = 40
EM_X86_64  = 62
EM_AARCH64 = 183
EM_RISCV   = 243

EM_NAMES = {
    EM_386:     "x86",
    EM_PPC:     "ppc",
    EM_PPC64:   "ppc64",
    EM_ARM:     "arm",
    EM_X86_64:  "x86_64",
    EM_AARCH64: "aarch64",
    EM_RISCV:   "riscv",
}

# ---------------------------------------------------------------------------
# Linux → FreeBSD provider map
# Key: basename (lower-case) of the Linux binary.
# Value: FreeBSD provider string:  pkg:<name>  | port:<origin>  | build:<id>
#
# Moved to data 2026-07-22 (roadmap 0.3 — "the registry becomes data, not
# code"): the table itself now lives in providers/provider-map.json
# (schema: providers/registry.schema.json), loaded once at import by
# providers/__init__.py and re-exported here as PROVIDER_MAP so existing
# callers (probe.propose_native, probe.classify_role, and any test doing
# `from probe.probe import PROVIDER_MAP`) keep working unchanged.
# EXTENSIBLE: add rows to providers/provider-map.json; probe picks the first
# matching key.
#
# NOTE on a deliberate omission: "xz" is NOT in provider-map.json.
# Removed 2026-07-19 — confirmed live via `pkg rquery` on FreeBSD 15.1: there
# is no pkg named "xz" (it's shipped in the FreeBSD BASE system at
# /usr/bin/xz, not installable via pkg(8) at all). bakery's native-base
# dataset is populated ONLY by pkg/port installs (no base.txz extraction
# happens anywhere in this pipeline), so a bare host path isn't reachable
# from inside it either — this needs a real "already in the FreeBSD base
# system" provider scheme (distinct from pkg:/port:/build:) to do properly;
# until that exists, leave xz unmapped so it correctly falls back to
# status=linuxulator (already a proven path) instead of failing the whole
# provisioning plan outright.
# ---------------------------------------------------------------------------

# Basenames that are definitively "load-bearing" regardless of size heuristic
LOAD_BEARING_NAMES: frozenset[str] = frozenset({
    # compilers
    "gcc", "g++", "clang", "clang++", "cc", "c++",
    # cross compilers (any xtensa-* or riscv32-*)
    # matched by prefix below
    # interpreters / runtimes
    "python", "python3", "python3.11", "python3.10", "python3.12",
    "node", "nodejs", "ruby", "perl", "perl5", "java", "lua",
    # build systems
    "cmake", "ninja", "make", "gmake", "meson", "bazel",
    # linkers
    "ld", "ld.bfd", "ld.gold", "lld",
    # esphome direct entry
    "esphome",
})

LOAD_BEARING_PREFIXES: tuple[str, ...] = (
    "xtensa-", "riscv32-esp-", "riscv64-", "arm-", "aarch64-",
)

# Size threshold in bytes: native ELF > this → probably load-bearing
LOAD_BEARING_SIZE_BYTES = 1 * 1024 * 1024  # 1 MiB

# Directories probed for executables (relative to rootfs)
EXEC_SEARCH_DIRS: list[str] = [
    "usr/bin", "usr/sbin", "usr/local/bin", "usr/local/sbin",
    "bin", "sbin",
    "usr/lib", "usr/local/lib",   # occasional co-located executables
    "usr/libexec", "usr/local/libexec",
]

# Also follow .platformio / home dirs for embedded toolchains
EXTENDED_SEARCH_PATTERNS: list[str] = [
    ".platformio",
    ".espressif",
]


# ---------------------------------------------------------------------------
# ELF header reader — pure Python, zero dependencies
# ---------------------------------------------------------------------------

# ELFInfo: holds the three ELF e_ident fields needed for ABI classification;
#   valid=False means the file was not an ELF or could not be read.
class ELFInfo:
    """Minimal ELF header fields."""
    __slots__ = ("osabi", "ei_class", "machine", "valid")

    # ELFInfo.__init__: initialises all slots to sentinel values; valid=False marks unread state
    def __init__(self) -> None:
        self.valid   = False
        self.osabi   = -1
        self.ei_class = -1
        self.machine  = -1


# read_elf_header:start
#   purpose: open a file and parse the first 20 bytes as an ELF e_ident + e_machine header
#   input:
#     path: Path — absolute path to the candidate binary on the host filesystem
#   output:
#     info: ELFInfo — populated header fields; info.valid=False if not an ELF or unreadable
#   sideEffects: opens and reads up to 20 bytes from path (read-only file I/O);
#                OSError/PermissionError are silently swallowed and return info.valid=False
def read_elf_header(path: Path) -> ELFInfo:
    """
    Read just enough of an ELF header to determine OSABI and e_machine.
    Returns an ELFInfo; info.valid is False if the file is not an ELF.

    Layout (elf(5), man7.org/linux/man-pages/man5/elf.5.html):
      Offset  Size  Field
      0       4     e_ident magic \\x7fELF
      4       1     EI_CLASS  (1=32-bit, 2=64-bit)
      5       1     EI_DATA   (1=LE, 2=BE)
      6       1     EI_VERSION
      7       1     EI_OSABI
      8       8     EI_ABIVERSION + padding
      16      2     e_type
      18      2     e_machine
    Total: first 20 bytes covers everything we need.
    """
    info = ELFInfo()
    # START_READ_ELF_BYTES
    try:
        with open(path, "rb") as fh:
            header = fh.read(20)
    except (OSError, PermissionError):
        return info

    if len(header) < 20:
        return info
    if header[:4] != ELFMAG:
        return info
    # END_READ_ELF_BYTES

    # START_DECODE_ELF_FIELDS
    info.valid    = True
    info.ei_class = header[EI_CLASS]
    info.osabi    = header[EI_OSABI]

    # e_machine endianness depends on EI_DATA
    ei_data = header[EI_DATA]
    fmt = "<H" if ei_data == 1 else ">H"
    (info.machine,) = struct.unpack_from(fmt, header, 18)
    # END_DECODE_ELF_FIELDS
    return info
# read_elf_header:end


# classify_elf:start
#   purpose: map a parsed ELFInfo to one of three ABI label strings
#   input:
#     info: ELFInfo — header previously returned by read_elf_header
#   output:
#     abi: str — one of "linux" | "freebsd" | "unknown"
#   sideEffects: none
#   rationale: OSABI=0 (NONE/SysV) is treated as "linux" because mainstream Linux
#              distros do not set the OSABI byte; the kernel/distro brand is
#              inferred from context (rootfs layout), not from the header alone.
def classify_elf(info: ELFInfo) -> str:
    """Map ELFInfo → abi string: linux | freebsd | unknown."""
    if not info.valid:
        return "unknown"
    if info.osabi == ELFOSABI_FREEBSD:
        return "freebsd"
    if info.osabi in (ELFOSABI_GNU, ELFOSABI_NONE):
        # OSABI=0 (NONE/SysV) is the most common Linux convention;
        # the kernel/distro brand is inferred from context (rootfs layout).
        return "linux"
    # Other OSABI values: NetBSD, Solaris, etc. — report as unknown.
    return "unknown"
# classify_elf:end


# read_shebang:start
#   purpose: extract the interpreter path from the first line of a script file
#   input:
#     path: Path — absolute path to the candidate script on the host filesystem
#   output:
#     interpreter: str | None — first token after "#!" (e.g. "/usr/bin/env"),
#                              or None if not a shebang script or unreadable
#   sideEffects: opens and reads up to 512 bytes from path (read-only file I/O);
#                OSError/PermissionError are silently swallowed and return None
def read_shebang(path: Path) -> str | None:
    """Return the interpreter from a #! line, or None."""
    try:
        with open(path, "rb") as fh:
            first = fh.read(512)
    except (OSError, PermissionError):
        return None
    if not first.startswith(b"#!"):
        return None
    nl = first.find(b"\n")
    line = first[2:nl].decode("ascii", errors="replace").strip() if nl > 0 else first[2:].decode("ascii", errors="replace").strip()
    return line.split()[0] if line else None
# read_shebang:end


# is_executable_file:start
#   purpose: confirm a path is a regular file with at least one executable bit set
#   input:
#     path: Path — filesystem path to test (may be a symlink)
#   output:
#     result: bool — True only if the file is regular (not device/socket/pipe)
#                   and has at least one x bit in its mode
#   sideEffects: calls path.stat() which performs a stat(2) syscall;
#                OSError (broken symlink, permission denied) is caught and returns False
def is_executable_file(path: Path) -> bool:
    """True if path is a regular (or symlinked) executable file."""
    try:
        st = path.stat()
    except OSError:
        return False
    if not (st.st_mode & 0o111):
        return False
    # Skip device files, sockets, etc.
    import stat as _stat
    return _stat.S_ISREG(st.st_mode)
# is_executable_file:end


# ---------------------------------------------------------------------------
# Role heuristic
# ---------------------------------------------------------------------------

# classify_role:start
#   purpose: assign a load-bearing / auxiliary / unknown role to an executable
#             using name lookup, prefix matching, provider-map membership, and size
#   input:
#     path: Path — absolute host path to the binary (used for name and stat)
#     abi: str   — ABI label from classify_elf or "script" / "unknown"
#     elf_info: ELFInfo — header (used only for the abi="linux"/"freebsd" size check)
#   output:
#     role: str — one of "load-bearing" | "auxiliary" | "unknown"
#   sideEffects: may call path.stat() (one stat(2) syscall) for the size heuristic
#                on linux/freebsd ELF files; OSError is caught silently
def classify_role(path: Path, abi: str, elf_info: ELFInfo) -> str:
    """Heuristic role: load-bearing | auxiliary | unknown."""
    name = path.name.lower()

    # START_ROLE_SCRIPT_UNKNOWN
    # Scripts are auxiliary unless their interpreter matches a known LB name
    if abi == "script":
        return "auxiliary"

    # Unknown (not ELF, not script): unknown
    if abi == "unknown":
        return "unknown"
    # END_ROLE_SCRIPT_UNKNOWN

    # START_ROLE_NAME_LOOKUP
    # Check explicit load-bearing name set
    if name in LOAD_BEARING_NAMES:
        return "load-bearing"

    # Check load-bearing prefixes (cross compilers)
    for prefix in LOAD_BEARING_PREFIXES:
        if name.startswith(prefix):
            return "load-bearing"

    # Check provider map — if we can natively substitute it, it matters
    if name in PROVIDER_MAP:
        return "load-bearing"
    # END_ROLE_NAME_LOOKUP

    # START_ROLE_SIZE_HEURISTIC
    # Large native ELF in a PATH dir → probably load-bearing
    if abi in ("linux", "freebsd"):
        try:
            size = path.stat().st_size
            if size >= LOAD_BEARING_SIZE_BYTES:
                return "load-bearing"
        except OSError:
            pass
    # END_ROLE_SIZE_HEURISTIC

    return "auxiliary"
# classify_role:end


# ---------------------------------------------------------------------------
# Status + native proposal
# ---------------------------------------------------------------------------

# propose_native:start
#   purpose: look up the FreeBSD pkg/port provider for a Linux binary basename
#   input:
#     name: str — basename of the Linux binary (case-insensitive lookup)
#   output:
#     native_block: dict | None — {"provider": "<pkg|port>:<name>", "artifact_path": None,
#                                 "verification": "guessed"} if a provider is known;
#                                 None otherwise
#   sideEffects: none
#   rationale: verification="guessed" (L0 of the schema's verification ladder) is
#              stamped unconditionally here — at probe time a provider is always
#              derived purely from the binary's name, never checked against a real
#              FreeBSD host. bakery(S4) may bump this to "exists" (L1) once the
#              artifact_path is confirmed present; higher levels (runs/behaves/proven)
#              are only ever produced by a future agent verification harness.
def propose_native(name: str) -> dict | None:
    """Return native block if we have a provider; else None."""
    key = name.lower()
    provider = PROVIDER_MAP.get(key)
    if provider is None:
        return None
    return {"provider": provider, "artifact_path": None, "verification": "guessed"}
# propose_native:end


# classify_status:start
#   purpose: derive the initial migration status from ABI label and provider availability
#   input:
#     abi: str         — ABI label ("freebsd" | "linux" | "script" | "unknown")
#     name: str        — binary basename (unused in current logic; reserved for future rules)
#     native: dict | None — result of propose_native; non-None means a FreeBSD substitute exists
#   output:
#     status: str — one of "native" | "linuxulator" | "unknown"
#   sideEffects: none
#   rationale: scripts are classified "native" because shell scripts are generally
#              portable; the bakery stage may downgrade this if the shebang interpreter
#              is itself a linuxulator binary.
def classify_status(
    abi: str, name: str, native: dict | None
) -> str:
    """
    Assign initial status:
      freebsd ELF           → native (trivially; already the right ABI)
      linux + known provider → native (candidate; bakery fills artifact_path)
      linux + no provider    → linuxulator
      script                 → native (run host interpreter) — or linuxulator if
                               the shebang interpreter is a Linux-only binary
      unknown                → unknown
    """
    if abi == "freebsd":
        return "native"
    if abi == "linux":
        return "native" if native else "linuxulator"
    if abi == "script":
        return "native"  # shell scripts are generally portable
    return "unknown"
# classify_status:end


# ---------------------------------------------------------------------------
# Filesystem walk
# ---------------------------------------------------------------------------

# iter_executables:start
#   purpose: yield all candidate executable files under a rootfs directory,
#            covering both canonical PATH directories and embedded-toolchain trees
#   input:
#     rootfs: Path — resolved absolute path to the unpacked container rootfs
#   output:
#     yields: tuple[Path, str] — (absolute_host_path, rootfs_relative_path)
#             for every regular executable file found
#   sideEffects: performs recursive directory traversal via iterdir() and rglob();
#                calls entry.resolve() and entry.stat() for each candidate;
#                skips proc/sys/dev/run to avoid virtual mount hangs;
#                silently ignores OSError/PermissionError on individual entries
def iter_executables(rootfs: Path):
    """
    Yield (absolute_host_path, rootfs_relative_path) for all candidate
    executables under the rootfs.  Follows symlinks that stay inside rootfs;
    skips proc/sys/dev virtual mounts.
    """
    SKIP_DIRS = {"proc", "sys", "dev", "run"}

    # CONTRACT: iterdir entries -> skip virtual/escaped -> recurse dirs | yield executables
    def _walk(start: Path, rel_base: str):
        try:
            entries = list(start.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            # Prevent escaping rootfs via symlink
            try:
                resolved.relative_to(rootfs.resolve())
            except ValueError:
                continue
            if entry.is_dir():
                _walk(entry, f"{rel_base}/{entry.name}")
            elif is_executable_file(entry):
                image_path = f"{rel_base}/{entry.name}"
                yield entry, image_path

    # START_WALK_PATH_DIRS
    # Walk canonical PATH dirs first
    for rel_dir in EXEC_SEARCH_DIRS:
        candidate = rootfs / rel_dir
        if candidate.exists():
            yield from _walk(candidate, f"/{rel_dir}")
    # END_WALK_PATH_DIRS

    # START_WALK_EXTENDED_PATTERNS
    # Walk extended patterns (PlatformIO, ESP-IDF toolchains, home dirs)
    for pattern in EXTENDED_SEARCH_PATTERNS:
        for candidate in rootfs.rglob(pattern):
            if candidate.is_dir():
                yield from _walk(candidate, "/" + str(candidate.relative_to(rootfs)))
    # END_WALK_EXTENDED_PATTERNS
# iter_executables:end


# ---------------------------------------------------------------------------
# Risk heuristic for the linuxulator block
# ---------------------------------------------------------------------------

# compute_risk:start
#   purpose: produce a coarse risk label for the linuxulator section of the manifest
#   input:
#     linuxulator_binaries: list[dict] — subset of binary entries whose status=="linuxulator"
#   output:
#     risk: str — one of "none" | "low" | "medium" | "high"
#   sideEffects: none
#   examples:
#     [] -> "none"
#     [{"role": "auxiliary"}] -> "low"
#     [{"role": "load-bearing"}] -> "high"
#     [{"role": "auxiliary"}, {"role": "auxiliary"}, {"role": "auxiliary"}] -> "medium"
def compute_risk(linuxulator_binaries: list[dict]) -> str:
    """
    Coarse risk signal based on count and role of binaries staying linuxulator.
      0               → none
      1-2 auxiliary   → low
      any load-bearing → high
      otherwise       → medium
    """
    if not linuxulator_binaries:
        return "none"
    lb_count = sum(1 for b in linuxulator_binaries if b.get("role") == "load-bearing")
    if lb_count > 0:
        return "high"
    total = len(linuxulator_binaries)
    return "low" if total <= 2 else "medium"
# compute_risk:end


# ---------------------------------------------------------------------------
# Main probe logic
# ---------------------------------------------------------------------------

# probe:start
#   purpose: walk a rootfs, classify every executable, and assemble a Substitution Manifest dict
#   input:
#     rootfs_dir: str   — path to the unpacked rootfs directory (resolved internally)
#     image_ref: str    — OCI image reference used as the manifest "image" label (may be empty)
#     snapshot_id: str  — optional ZFS snapshot ID stored in manifest["rootfs_snapshot"]
#   output:
#     manifest: dict — Substitution Manifest conforming to
#                      schemas/substitution-manifest.schema.json; keys: image, binaries,
#                      linuxulator (+ rootfs_snapshot when snapshot_id given)
#   sideEffects: calls iter_executables which performs recursive filesystem traversal
#                (stat + open reads) over rootfs_dir; raises SystemExit if rootfs_dir
#                does not exist or is not a directory
#   usedBy: main
def probe(rootfs_dir: str, image_ref: str = "", snapshot_id: str = "") -> dict:
    """
    Walk rootfs, classify all executables, return a Substitution Manifest dict.
    """
    rootfs = Path(rootfs_dir).resolve()
    if not rootfs.is_dir():
        raise SystemExit(f"rootfs not found: {rootfs_dir}")

    seen_paths: set[str] = set()
    binaries: list[dict] = []

    for host_path, image_path in iter_executables(rootfs):
        if image_path in seen_paths:
            continue
        seen_paths.add(image_path)

        name = host_path.name

        # START_CLASSIFY_ABI
        elf_info = read_elf_header(host_path)
        if elf_info.valid:
            abi = classify_elf(elf_info)
            interp = None
        else:
            interp = read_shebang(host_path)
            abi = "script" if interp else "unknown"
        # END_CLASSIFY_ABI

        # START_CLASSIFY_ROLE_STATUS
        role = classify_role(host_path, abi, elf_info)

        # native proposal only for linux ABI — freebsd ELF needs no substitute
        native = propose_native(name) if abi == "linux" else None

        status = classify_status(abi, name, native)
        # END_CLASSIFY_ROLE_STATUS

        # START_ASSEMBLE_BINARY_ENTRY
        entry: dict = {
            "path":             image_path,
            "role":             role,
            "abi":              abi,
            "status":           status,
            "syscalls_needed":  [],
            "notes":            "",
        }
        if native:
            entry["native"] = native

        # Annotate machine architecture in notes for human readers
        if elf_info.valid:
            mname = EM_NAMES.get(elf_info.machine, f"e_machine={elf_info.machine:#06x}")
            entry["notes"] = f"ELF OSABI={elf_info.osabi} machine={mname}"
        elif interp:
            entry["notes"] = f"shebang: {interp}"

        binaries.append(entry)
        # END_ASSEMBLE_BINARY_ENTRY

    # START_BUILD_LINUXULATOR_BLOCK
    lx_binaries = [b for b in binaries if b["status"] == "linuxulator"]
    lx_required  = len(lx_binaries) > 0
    lx_risk      = compute_risk(lx_binaries)

    manifest: dict = {
        "image":      image_ref or rootfs_dir,
        "binaries":   binaries,
        "linuxulator": {
            "required": lx_required,
            "gaps":     [],   # filled by smoke.freebsd.sh output
            "risk":     lx_risk,
        },
    }
    if snapshot_id:
        manifest["rootfs_snapshot"] = snapshot_id
    # END_BUILD_LINUXULATOR_BLOCK

    return manifest
# probe:end


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# main:start
#   purpose: parse CLI arguments, invoke probe, and write the manifest to stdout or a file
#   input:
#     (no parameters; reads sys.argv via argparse)
#   output:
#     (no return value; exits 0 on success, non-zero on error)
#   sideEffects: reads sys.argv; calls probe() which traverses rootfs;
#                either prints JSON to stdout or writes JSON to args.out via
#                Path(args.out).write_text(); prints a confirmation line to stderr
#                when writing to a file; may raise SystemExit on missing rootfs
def main() -> None:
    # START_PARSE_CLI_ARGS
    parser = argparse.ArgumentParser(
        description="jailrun probe: classify rootfs executables → substitution manifest"
    )
    parser.add_argument("rootfs", help="unpacked rootfs directory")
    parser.add_argument("--image",    default="", help="OCI image reference (label)")
    parser.add_argument("--snapshot", default="", help="S3 ZFS snapshot_id")
    parser.add_argument("--out",      default="-", help="output path (default: stdout)")
    args = parser.parse_args()
    # END_PARSE_CLI_ARGS

    # START_RUN_PROBE_AND_EMIT
    manifest = probe(args.rootfs, args.image, args.snapshot)
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()

    text = json.dumps(manifest, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
        print(f"Manifest written to {args.out}", file=sys.stderr)
    # END_RUN_PROBE_AND_EMIT
# main:end


if __name__ == "__main__":
    main()

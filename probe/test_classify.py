#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: probe/test_classify.py
# PURPOSE: unit tests for ELF OSABI classifier in probe.py using in-memory fake ELF headers
# INTENT: verifies read_elf_header + classify_elf correctly identify linux/freebsd/unknown from raw EI_OSABI/e_machine bytes; runnable on Linux host without FreeBSD
# DEPENDENCIES: stdlib (struct, sys, tempfile, os, pathlib), probe (read_elf_header, classify_elf, ELF constants)
# PUBLIC_API: run_all, TESTS list; each test_* function is also callable directly by pytest
# END_AI_HEADER
"""
test_classify.py — unit tests for the ELF classifier in probe.py.
Builds minimal fake ELF headers in-memory and asserts correct OSABI detection.

Run on host (Linux/linux-host) — no FreeBSD required:
    python3 -m pytest probe/test_classify.py -v
    # or directly:
    python3 probe/test_classify.py
"""

import struct
import sys
import tempfile
import os
from pathlib import Path

# Make sure we can import probe from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from probe import (
    read_elf_header,
    classify_elf,
    ELFMAG,
    ELFOSABI_NONE,
    ELFOSABI_GNU,
    ELFOSABI_FREEBSD,
    ELFOSABI_NETBSD,
    EM_X86_64,
    EM_AARCH64,
    EM_386,
)


# ---------------------------------------------------------------------------
# Helpers to build fake ELF headers
# ---------------------------------------------------------------------------

# make_elf_header:start
#   purpose: construct a minimal syntactically valid 64-byte ELF header bytes object for testing
#   input:
#     osabi: int — EI_OSABI byte (e.g. ELFOSABI_GNU=3, ELFOSABI_FREEBSD=9)
#     machine: int — e_machine value (e.g. EM_X86_64=62, EM_AARCH64=183)
#     ei_class: int — EI_CLASS byte; 2=ELFCLASS64, 1=ELFCLASS32 (default 2)
#     ei_data: int — EI_DATA byte; 1=LE, 2=BE (default 1); controls struct pack byte order
#   output:
#     header: bytes — 64-byte buffer; first 20 bytes cover the range read_elf_header() parses
#   sideEffects: none
def make_elf_header(
    osabi: int,
    machine: int,
    ei_class: int = 2,   # 2 = ELFCLASS64
    ei_data: int = 1,    # 1 = ELFDATA2LSB (little-endian)
) -> bytes:
    """
    Build a minimal 64-byte ELF header (e_ident[16] + 4× u16 fields).
    We only need the first 20 bytes to cover what read_elf_header() reads,
    but we pad to 64 so the file is at least ELF-shaped.

    e_ident layout (first 16 bytes):
      [0..3]  = 0x7f 'E' 'L' 'F'
      [4]     = EI_CLASS
      [5]     = EI_DATA
      [6]     = EI_VERSION (1)
      [7]     = EI_OSABI
      [8..15] = padding / ABI version
    Followed by:
      [16..17] e_type   (u16)
      [18..19] e_machine (u16)
    """
    e_ident = bytearray(16)
    e_ident[0:4]  = ELFMAG
    e_ident[4]    = ei_class
    e_ident[5]    = ei_data
    e_ident[6]    = 1          # EI_VERSION = EV_CURRENT
    e_ident[7]    = osabi
    # bytes 8..15: EI_ABIVERSION + padding, leave as zero

    fmt = "<HH" if ei_data == 1 else ">HH"
    e_type_machine = struct.pack(fmt, 2, machine)  # ET_EXEC=2

    header = bytes(e_ident) + e_type_machine
    # Pad to 64 bytes
    header += b"\x00" * (64 - len(header))
    return header
# make_elf_header:end


# write_temp_elf:start
#   purpose: materialise a fake ELF header onto disk as a named temp file for use by read_elf_header()
#   input:
#     osabi: int — EI_OSABI value forwarded to make_elf_header
#     machine: int — e_machine value forwarded to make_elf_header
#     **kwargs: passed through to make_elf_header (ei_class, ei_data)
#   output:
#     path: Path — absolute path to the temp .elf file, chmod 0o755
#   sideEffects: creates a temp file via tempfile.mkstemp; caller is responsible for unlinking it
def write_temp_elf(osabi: int, machine: int, **kwargs) -> Path:
    """Write a fake ELF to a temp file; return path."""
    data = make_elf_header(osabi, machine, **kwargs)
    fd, name = tempfile.mkstemp(suffix=".elf")
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    p = Path(name)
    p.chmod(0o755)
    return p
# write_temp_elf:end


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# CONTRACT: write ELF with ELFOSABI_GNU + EM_X86_64 -> read_elf_header -> classify_elf returns 'linux'
def test_linux_gnu_osabi():
    """EI_OSABI = 3 (ELFOSABI_GNU/LINUX) → abi='linux'."""
    p = write_temp_elf(ELFOSABI_GNU, EM_X86_64)
    try:
        info = read_elf_header(p)
        assert info.valid, "ELF should be valid"
        assert info.osabi == ELFOSABI_GNU
        assert info.machine == EM_X86_64
        assert classify_elf(info) == "linux", f"expected 'linux', got {classify_elf(info)!r}"
    finally:
        p.unlink()
    print("PASS test_linux_gnu_osabi")


# CONTRACT: write ELF with ELFOSABI_NONE (0) + EM_X86_64 -> classify_elf returns 'linux' (SysV=0 convention)
def test_linux_none_osabi():
    """
    EI_OSABI = 0 (ELFOSABI_NONE / SysV) is the overwhelmingly common convention
    for Linux ELFs — classify_elf() must treat it as 'linux'.
    """
    p = write_temp_elf(ELFOSABI_NONE, EM_X86_64)
    try:
        info = read_elf_header(p)
        assert info.valid
        assert info.osabi == ELFOSABI_NONE
        result = classify_elf(info)
        assert result == "linux", f"expected 'linux' (OSABI=0 convention), got {result!r}"
    finally:
        p.unlink()
    print("PASS test_linux_none_osabi")


# CONTRACT: write ELF with ELFOSABI_FREEBSD (9) + EM_X86_64 -> classify_elf returns 'freebsd'
def test_freebsd_osabi():
    """EI_OSABI = 9 (ELFOSABI_FREEBSD) → abi='freebsd'."""
    p = write_temp_elf(ELFOSABI_FREEBSD, EM_X86_64)
    try:
        info = read_elf_header(p)
        assert info.valid
        assert info.osabi == ELFOSABI_FREEBSD
        result = classify_elf(info)
        assert result == "freebsd", f"expected 'freebsd', got {result!r}"
    finally:
        p.unlink()
    print("PASS test_freebsd_osabi")


# CONTRACT: write ELF with ELFOSABI_NETBSD (2) -> classify_elf returns 'unknown' (unrecognised ABI)
def test_netbsd_osabi_unknown():
    """EI_OSABI = 2 (ELFOSABI_NETBSD) → abi='unknown' (not Linux, not FreeBSD)."""
    p = write_temp_elf(ELFOSABI_NETBSD, EM_X86_64)
    try:
        info = read_elf_header(p)
        assert info.valid
        result = classify_elf(info)
        assert result == "unknown", f"expected 'unknown', got {result!r}"
    finally:
        p.unlink()
    print("PASS test_netbsd_osabi_unknown")


# CONTRACT: write LE ELF with EM_AARCH64 -> read_elf_header correctly parses e_machine as 183
def test_machine_field_aarch64():
    """e_machine = 183 (EM_AARCH64) is parsed correctly from LE header."""
    p = write_temp_elf(ELFOSABI_NONE, EM_AARCH64, ei_data=1)
    try:
        info = read_elf_header(p)
        assert info.valid
        assert info.machine == EM_AARCH64, f"expected {EM_AARCH64}, got {info.machine}"
    finally:
        p.unlink()
    print("PASS test_machine_field_aarch64")


# CONTRACT: write BE ELF (EI_DATA=2) with EM_AARCH64 -> read_elf_header uses big-endian struct unpack -> machine==183 and classify returns 'freebsd'
def test_machine_field_big_endian():
    """Big-endian ELF (EI_DATA=2): e_machine is parsed with BE byte order."""
    p = write_temp_elf(ELFOSABI_FREEBSD, EM_AARCH64, ei_data=2)
    try:
        info = read_elf_header(p)
        assert info.valid
        assert info.machine == EM_AARCH64, (
            f"BE parse failed: expected {EM_AARCH64}, got {info.machine}"
        )
        assert classify_elf(info) == "freebsd"
    finally:
        p.unlink()
    print("PASS test_machine_field_big_endian")


# CONTRACT: write shell-script bytes to temp file -> read_elf_header returns valid=False -> classify_elf returns 'unknown'
def test_not_elf_file():
    """A shell script is NOT an ELF; read_elf_header() returns valid=False."""
    fd, name = tempfile.mkstemp(suffix=".sh")
    try:
        os.write(fd, b"#!/bin/sh\necho hello\n")
        os.close(fd)
        p = Path(name)
        info = read_elf_header(p)
        assert not info.valid, "Script should not be recognised as ELF"
        assert classify_elf(info) == "unknown"
    finally:
        Path(name).unlink()
    print("PASS test_not_elf_file")


# CONTRACT: write 8 bytes (ELF magic + 4 bytes) -> read_elf_header returns valid=False without raising
def test_truncated_file():
    """A file shorter than 20 bytes → valid=False (no crash)."""
    fd, name = tempfile.mkstemp()
    try:
        os.write(fd, ELFMAG + b"\x02\x01\x01\x09")  # 8 bytes only
        os.close(fd)
        p = Path(name)
        info = read_elf_header(p)
        assert not info.valid, "Truncated ELF-magic file should be invalid"
    finally:
        Path(name).unlink()
    print("PASS test_truncated_file")


# CONTRACT: write ELF with EI_CLASS=1 (32-bit) + EM_386 -> read_elf_header sets ei_class=1, machine=3 -> classify_elf returns 'linux'
def test_32bit_elf():
    """EI_CLASS = 1 (32-bit) ELF is also handled."""
    p = write_temp_elf(ELFOSABI_NONE, EM_386, ei_class=1)
    try:
        info = read_elf_header(p)
        assert info.valid
        assert info.ei_class == 1
        assert info.machine == EM_386
        assert classify_elf(info) == "linux"
    finally:
        p.unlink()
    print("PASS test_32bit_elf")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_linux_gnu_osabi,
    test_linux_none_osabi,
    test_freebsd_osabi,
    test_netbsd_osabi_unknown,
    test_machine_field_aarch64,
    test_machine_field_big_endian,
    test_not_elf_file,
    test_truncated_file,
    test_32bit_elf,
]


# run_all:start
#   purpose: execute every function in TESTS, collect failures, report pass/fail counts
#   input: none
#   output: none (results printed to stdout)
#   sideEffects: prints PASS/FAIL/ERROR lines per test; calls sys.exit(1) if any failure; calls sys.exit implicitly exits 0 on full pass via normal return
def run_all():
    failures = []
    for fn in TESTS:
        try:
            fn()
        except AssertionError as exc:
            print(f"FAIL {fn.__name__}: {exc}")
            failures.append(fn.__name__)
        except Exception as exc:
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
            failures.append(fn.__name__)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print(f"All {len(TESTS)} tests passed.")
# run_all:end


if __name__ == "__main__":
    run_all()

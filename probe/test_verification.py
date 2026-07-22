#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: probe/test_verification.py
# PURPOSE: unit tests for the L0-L4 verification ladder stamped by probe(S2) on
#          proposed native substitutions (schemas/substitution-manifest.schema.json)
# INTENT: milestone 0.3's data-model foundation — a native substitution's
#         trustworthiness must be explicit and never conflated with "we checked
#         it". probe(S2) only ever produces the weakest level, "guessed" (L0):
#         a provider/path derived purely from the binary's name, never checked
#         on a real host. These tests pin that behavior down so a future change
#         can't silently start claiming a stronger level than probe actually
#         verified.
# DEPENDENCIES: stdlib (struct, sys, tempfile, pathlib), probe (propose_native, probe)
# PUBLIC_API: run_all, TESTS list; each test_* function is also callable directly by pytest
# END_AI_HEADER
"""
test_verification.py — unit tests for probe.py's native.verification stamping.

Run on host (Linux/linux-host) — no FreeBSD required:
    python3 -m pytest probe/test_verification.py -v
    # or directly:
    python3 probe/test_verification.py
"""

import struct
import sys
import tempfile
from pathlib import Path

# Make sure we can import probe from the same directory
sys.path.insert(0, str(Path(__file__).parent))

from probe import probe, propose_native


# ---------------------------------------------------------------------------
# Helper: minimal fake ELF header (mirrors test_classify.py / the CI schema
# validation script's own _write_fake_elf helper)
# ---------------------------------------------------------------------------

# write_fake_elf:start
#   purpose: materialise a minimal syntactically valid ELF header at `path`
#   input:
#     path: Path — destination file path (parent dirs must already exist)
#     osabi: int — EI_OSABI byte (0 = SysV/Linux convention, 3 = ELFOSABI_GNU/Linux)
#   output: none
#   sideEffects: writes a 64-byte file at `path` and chmods it 0o755 (executable)
def write_fake_elf(path: Path, osabi: int = 0) -> None:
    """Write a minimal 64-byte Linux ELF header (EM_X86_64) to `path`."""
    e_ident = bytearray(16)
    e_ident[0:4] = b"\x7fELF"
    e_ident[4] = 2       # ELFCLASS64
    e_ident[5] = 1       # little-endian
    e_ident[6] = 1       # EI_VERSION
    e_ident[7] = osabi
    header = bytes(e_ident) + struct.pack("<HH", 2, 62)  # ET_EXEC, EM_X86_64
    header += b"\x00" * (64 - len(header))
    path.write_bytes(header)
    path.chmod(0o755)
# write_fake_elf:end


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# CONTRACT: propose_native() for a basename with a known provider stamps verification="guessed"
def test_propose_native_stamps_guessed():
    """A known provider name (cmake) proposes a native block at L0 ('guessed')."""
    native = propose_native("cmake")
    assert native is not None, "cmake has a known provider in PROVIDER_MAP"
    assert native["provider"] == "pkg:cmake"
    assert native["artifact_path"] is None
    assert native["verification"] == "guessed", (
        f"expected verification='guessed', got {native.get('verification')!r}"
    )
    print("PASS test_propose_native_stamps_guessed")


# CONTRACT: propose_native() for a basename with NO known provider returns None (no verification field to check)
def test_propose_native_unknown_name_returns_none():
    """A name with no entry in PROVIDER_MAP -> None, not a native block with some default level."""
    native = propose_native("some-tool-nobody-has-heard-of")
    assert native is None
    print("PASS test_propose_native_unknown_name_returns_none")


# CONTRACT: probe() on a rootfs with a Linux ELF named after a known provider (cmake) ->
#           the resulting binary entry's native["verification"] == "guessed"
def test_probe_end_to_end_sets_guessed_verification():
    """
    Full probe() walk: a Linux ELF named 'cmake' gets classified abi=linux,
    status=native (candidate), and its native block carries verification='guessed'
    — probe(S2) never checks a real host, so it can never claim more than L0.
    """
    with tempfile.TemporaryDirectory() as td:
        rootfs = Path(td)
        bindir = rootfs / "usr" / "bin"
        bindir.mkdir(parents=True)
        write_fake_elf(bindir / "cmake", osabi=0)  # ELFOSABI_NONE -> classified 'linux'

        manifest = probe(str(rootfs), image_ref="verification-test:latest")

        entries = [b for b in manifest["binaries"] if b["path"] == "/usr/bin/cmake"]
        assert len(entries) == 1, f"expected exactly one /usr/bin/cmake entry, got {entries}"
        entry = entries[0]
        assert entry["abi"] == "linux"
        assert "native" in entry, "cmake has a known provider — native block expected"
        assert entry["native"]["provider"] == "pkg:cmake"
        assert entry["native"]["verification"] == "guessed", (
            f"expected verification='guessed', got {entry['native'].get('verification')!r}"
        )
    print("PASS test_probe_end_to_end_sets_guessed_verification")


# CONTRACT: probe() on a Linux ELF with NO known provider -> no native block at all,
#           and therefore no verification field anywhere (status=linuxulator instead)
def test_probe_no_native_block_no_verification_when_no_provider():
    """A Linux ELF with an unmapped basename gets status=linuxulator and no native block."""
    with tempfile.TemporaryDirectory() as td:
        rootfs = Path(td)
        bindir = rootfs / "usr" / "bin"
        bindir.mkdir(parents=True)
        write_fake_elf(bindir / "some-linux-only-tool", osabi=3)  # ELFOSABI_GNU -> 'linux'

        manifest = probe(str(rootfs), image_ref="verification-test:latest")

        entries = [b for b in manifest["binaries"] if b["path"] == "/usr/bin/some-linux-only-tool"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["status"] == "linuxulator"
        assert "native" not in entry, "no provider known -> no native block should be added at all"
    print("PASS test_probe_no_native_block_no_verification_when_no_provider")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_propose_native_stamps_guessed,
    test_propose_native_unknown_name_returns_none,
    test_probe_end_to_end_sets_guessed_verification,
    test_probe_no_native_block_no_verification_when_no_provider,
]


# run_all:start
#   purpose: execute every function in TESTS, collect failures, report pass/fail counts
#   input: none
#   output: none (results printed to stdout)
#   sideEffects: prints PASS/FAIL/ERROR lines per test; calls sys.exit(1) if any failure
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

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: providers/test_registry_roundtrip.py
# PURPOSE: prove the providers/*.json data files, loaded through providers/__init__.py,
#          are EXACTLY the same tables probe.py and bakery.py used to hard-code inline —
#          the behavior-preserving guarantee for the roadmap 0.3 data-not-code extraction.
# INTENT: 2026-07-22 — PROVIDER_MAP (probe.py) and PKG_ARTIFACTS/PORT_ARTIFACTS/
#         MULTI_BINARY_PKGS (bakery.py) moved from Python dict/set literals to
#         schema-validated JSON under providers/. This test pastes the ORIGINAL
#         hard-coded literals as expected fixtures and asserts full equality against
#         what probe.probe.PROVIDER_MAP / bakery.bakery.PKG_ARTIFACTS / ... resolve to
#         today — so any future edit that silently drops/changes/mistypes an entry in
#         the JSON data files (or in the loader) fails loudly here.
# DEPENDENCIES: stdlib (os, sys, pathlib), providers (PROVIDER_MAP, PKG_ARTIFACTS,
#               PORT_ARTIFACTS, MULTI_BINARY_PKGS), probe.probe, bakery.bakery
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_registry_roundtrip.py — round-trip / behavior-preservation tests for providers/.

Run on host (no FreeBSD required):
    python3 -m pytest providers/test_registry_roundtrip.py -v
    # or directly:
    python3 providers/test_registry_roundtrip.py
"""

import os
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from providers import (  # noqa: E402
    MULTI_BINARY_PKGS,
    PKG_ARTIFACTS,
    PORT_ARTIFACTS,
    PROVIDER_MAP,
)

# ---------------------------------------------------------------------------
# Expected fixtures: pasted VERBATIM from probe/probe.py and bakery/bakery.py
# as they stood immediately before the providers/ extraction (2026-07-22).
# Do not "clean up" or reorder these — the point is an exact, independent copy
# to diff the loaded data against.
# ---------------------------------------------------------------------------

EXPECTED_PROVIDER_MAP: dict[str, str] = {
    # Python runtimes
    "python3":        "pkg:python311",
    "python3.11":     "pkg:python311",
    "python3.10":     "pkg:python310",
    "python3.12":     "pkg:python312",
    "python":         "pkg:python311",

    # Build systems
    "cmake":          "pkg:cmake",
    "ninja":          "pkg:ninja",
    "make":           "pkg:gmake",
    "gmake":          "pkg:gmake",
    "meson":          "pkg:meson",
    "bazel":          "pkg:bazel",
    "scons":          "pkg:scons",

    # Compilers / toolchains
    "gcc":            "pkg:gcc",
    "g++":            "pkg:gcc",
    "clang":          "pkg:llvm",
    "clang++":        "pkg:llvm",
    "ld":             "pkg:binutils",
    "ar":             "pkg:binutils",
    "objcopy":        "pkg:binutils",
    "strip":          "pkg:binutils",
    "nm":             "pkg:binutils",
    "readelf":        "pkg:binutils",

    # Xtensa ESP toolchain (the esphome load-bearing binary)
    "xtensa-esp32-elf-gcc":    "port:devel/xtensa-esp-elf",
    "xtensa-esp32-elf-g++":    "port:devel/xtensa-esp-elf",
    "xtensa-esp32-elf-ld":     "port:devel/xtensa-esp-elf",
    "xtensa-esp32-elf-objcopy":"port:devel/xtensa-esp-elf",
    "xtensa-esp32s2-elf-gcc":  "port:devel/xtensa-esp-elf",
    "xtensa-esp32s3-elf-gcc":  "port:devel/xtensa-esp-elf",
    "xtensa-lx106-elf-gcc":    "port:devel/xtensa-esp-elf",
    "riscv32-esp-elf-gcc":     "port:devel/riscv32-esp-elf",

    # Interpreters / runtimes
    "node":           "pkg:node",
    "nodejs":         "pkg:node",
    "ruby":           "pkg:ruby",
    "perl":           "pkg:perl5",
    "perl5":          "pkg:perl5",
    "php":            "pkg:php83",
    "lua":            "pkg:lua54",
    "java":           "pkg:openjdk21",

    # Shell & coreutils
    "bash":           "pkg:bash",
    "sh":             "pkg:bash",
    "dash":           "pkg:dash",
    "zsh":            "pkg:zsh",
    "curl":           "pkg:curl",
    "wget":           "pkg:wget",
    "git":            "pkg:git",
    "tar":            "pkg:gtar",
    "gzip":           "pkg:gzip",
    "bzip2":          "pkg:bzip2",
    # "xz" intentionally absent — see probe.py's comment above the PROVIDER_MAP
    # import for the full rationale (no pkg named "xz" on FreeBSD 15.1).
    "zip":            "pkg:zip",
    "unzip":          "pkg:unzip",

    # Libraries commonly surfacing as binaries
    "openssl":        "pkg:openssl",
    "sqlite3":        "pkg:sqlite3",

    # Package managers (likely linuxulator; map only if port exists)
    "pip":            "pkg:py311-pip",
    "pip3":           "pkg:py311-pip",

    # esphome ecosystem
    "esphome":        "pkg:py311-esphome",
}

EXPECTED_PKG_ARTIFACTS: dict[str, str] = {
    "python311":      "/usr/local/bin/python3.11",
    "python312":      "/usr/local/bin/python3.12",
    "python313":      "/usr/local/bin/python3.13",
    "cmake":          "/usr/local/bin/cmake",
    "ninja":          "/usr/local/bin/ninja",
    "git":            "/usr/local/bin/git",
    "gmake":          "/usr/local/bin/gmake",
    "bash":           "/usr/local/bin/bash",
    "curl":           "/usr/local/bin/curl",
    "wget":           "/usr/local/bin/wget",
    "rsync":          "/usr/local/bin/rsync",
    "gawk":           "/usr/local/bin/gawk",
    "gsed":           "/usr/local/bin/gsed",
    "perl5":          "/usr/local/bin/perl",
    "pkgconf":        "/usr/local/bin/pkgconf",
    "bison":          "/usr/local/bin/bison",
    "automake":       "/usr/local/bin/automake",
    "autoconf":       "/usr/local/bin/autoconf",
    "libtool":        "/usr/local/bin/libtool",
    "texinfo":        "/usr/local/bin/makeinfo",
    "help2man":       "/usr/local/bin/help2man",
    "gperf":          "/usr/local/bin/gperf",
    "zip":            "/usr/local/bin/zip",
    "m4":             "/usr/local/bin/gm4",
}

EXPECTED_PORT_ARTIFACTS: dict[str, str] = {
    "devel/xtensa-esp-elf": "/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-gcc",
    "devel/xtensa-esp32-elf": "/usr/local/xtensa-esp32-elf-idf52/bin/xtensa-esp32-elf-gcc",
    "devel/esp-quick-toolchain": (
        "/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-gcc"
    ),
}

EXPECTED_MULTI_BINARY_PKGS: frozenset[str] = frozenset({"binutils"})


# CONTRACT: providers.PROVIDER_MAP loaded from providers/provider-map.json is byte-for-byte
#           identical (keys, values, count, type) to the table that used to be hard-coded
#           in probe/probe.py.
def test_provider_map_round_trips_exactly():
    assert isinstance(PROVIDER_MAP, dict)
    assert len(PROVIDER_MAP) == 55 == len(EXPECTED_PROVIDER_MAP)
    assert PROVIDER_MAP == EXPECTED_PROVIDER_MAP


# CONTRACT: providers.PKG_ARTIFACTS loaded from providers/pkg-artifacts.json is identical
#           to bakery/bakery.py's former PKG_ARTIFACTS.
def test_pkg_artifacts_round_trips_exactly():
    assert isinstance(PKG_ARTIFACTS, dict)
    assert len(PKG_ARTIFACTS) == 24 == len(EXPECTED_PKG_ARTIFACTS)
    assert PKG_ARTIFACTS == EXPECTED_PKG_ARTIFACTS


# CONTRACT: providers.PORT_ARTIFACTS loaded from providers/port-artifacts.json is identical
#           to bakery/bakery.py's former PORT_ARTIFACTS.
def test_port_artifacts_round_trips_exactly():
    assert isinstance(PORT_ARTIFACTS, dict)
    assert len(PORT_ARTIFACTS) == 3 == len(EXPECTED_PORT_ARTIFACTS)
    assert PORT_ARTIFACTS == EXPECTED_PORT_ARTIFACTS


# CONTRACT: providers.MULTI_BINARY_PKGS loaded from providers/multi-binary-pkgs.json is
#           identical to bakery/bakery.py's former MULTI_BINARY_PKGS, AND is still a
#           frozenset (bakery.py's fill_artifact_paths only ever does membership tests —
#           a list would also "work" but the type must not silently change).
def test_multi_binary_pkgs_round_trips_exactly():
    assert isinstance(MULTI_BINARY_PKGS, frozenset)
    assert MULTI_BINARY_PKGS == EXPECTED_MULTI_BINARY_PKGS


# CONTRACT: probe.py and bakery.py must import (re-export) the SAME objects providers
#           exposes — not copies, not re-derived tables — so `probe.probe.PROVIDER_MAP`
#           and `bakery.bakery.PKG_ARTIFACTS` (the names existing tests already import)
#           keep resolving exactly as before the extraction.
#
# NOTE on import style: probe/ has no __init__.py (see probe/test_classify.py and
# schemas/test_substitution_manifest_schema.py's own comments on this) — whichever
# import style runs first ("import probe" with probe/ itself on sys.path, giving a
# flat top-level module, vs "import probe.probe" as a package submodule) pins
# sys.modules["probe"] for the rest of the process; mixing both styles in the SAME
# pytest session breaks the second one. This test file lives in the same session as
# probe/test_classify.py and probe/test_verification.py, which both already use the
# flat style — so it follows suit here instead of `from probe.probe import ...`
# (that package style is still exercised, in its own fresh process, by
# .github/scripts/validate_provider_data.py and validate_manifest_schema.py).
# bakery/ DOES have __init__.py, so `from bakery.bakery import ...` has no such
# ambiguity and matches bakery/test_verification_bump.py's own convention.
def test_probe_and_bakery_reexport_the_same_objects():
    _probe_dir = str(Path(__file__).parent.parent / "probe")
    if _probe_dir not in sys.path:
        sys.path.insert(0, _probe_dir)
    from probe import PROVIDER_MAP as probe_provider_map  # noqa: E402

    from bakery.bakery import (
        MULTI_BINARY_PKGS as bakery_multi_binary_pkgs,
        PKG_ARTIFACTS as bakery_pkg_artifacts,
        PORT_ARTIFACTS as bakery_port_artifacts,
    )

    assert probe_provider_map is PROVIDER_MAP
    assert bakery_pkg_artifacts is PKG_ARTIFACTS
    assert bakery_port_artifacts is PORT_ARTIFACTS
    assert bakery_multi_binary_pkgs is MULTI_BINARY_PKGS


# CONTRACT: the data files themselves live next to providers/__init__.py and are
#           discovered via Path(__file__).parent, not cwd — so the loader works no
#           matter where the caller's process cwd is.
def test_data_files_exist_next_to_loader_module():
    data_dir = Path(__file__).parent
    for name in (
        "provider-map.json",
        "pkg-artifacts.json",
        "port-artifacts.json",
        "multi-binary-pkgs.json",
        "registry.schema.json",
    ):
        assert (data_dir / name).is_file(), f"missing providers/{name}"


TESTS = [
    test_provider_map_round_trips_exactly,
    test_pkg_artifacts_round_trips_exactly,
    test_port_artifacts_round_trips_exactly,
    test_multi_binary_pkgs_round_trips_exactly,
    test_probe_and_bakery_reexport_the_same_objects,
    test_data_files_exist_next_to_loader_module,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

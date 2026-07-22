# START_AI_HEADER
# MODULE: bakery/bakery.py
# PURPOSE: S4 native-supply subsystem — resolves native.provider strings in a Substitution Manifest to concrete FreeBSD artifact paths and registers a provisioning base with S3 store.
# INTENT: Bridges S2 probe output (manifest with status=native binaries) and S3 store (ZFS base provisioning); sits between them so runtime S1 can consume a fully resolved manifest with snapshot_id.
# DEPENDENCIES: stdlib (copy, hashlib, json, logging, re, dataclasses, pathlib, typing); store.Store (mocked here — real impl in store/); external tools: pkg(8) install, ports make, crosstool-NG build (executed by store, not bakery directly)
# PUBLIC_API: bake(manifest) -> dict; build_plan(manifest) -> (ProvisioningPlan, list[str]); plan_to_provision_cmd(plan) -> str; fill_artifact_paths(manifest, plan) -> dict; parse_provider(provider) -> (kind, target); resolve_pkg/resolve_port/resolve_build
# END_AI_HEADER
"""
bakery.py — S4: native supply subsystem for jailrun.

Consumes a Substitution Manifest (schemas/substitution-manifest.schema.json).
For each binary with status=native and a native.provider, resolves the provider
to a real FreeBSD artifact and fills native.artifact_path.

Provider grammar:
  pkg:<name>     → pkg install <name>   (binary package from pkg.FreeBSD.org)
  port:<origin>  → build the port at /usr/ports/<origin>
  build:<id>     → a from-source recipe (crosstool-NG, etc.)

Produces a provisioning plan, then calls store.register_base(name, provision).
Returns the updated manifest with artifact_path filled and status confirmed.

NOTE: store import is MOCKED — S3 (store/) owns the real implementation.
      This module calls store.register_base() by its documented signature only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Store import — real S3 implementation, mock fallback for Linux-host testing.
#
# Fixed 2026-07-19: this module called an internal _MockStore
# UNCONDITIONALLY from day one — the "NOTE: store import is MOCKED... Replace
# this block with: from store.store import Store" comment sat here untouched
# through every prior "prove-out". Confirmed live: register_base() returned a
# fake "zroot/jailrun/bases/<hash>" snapshot_id (zroot is not even the real
# pool name), which engine.py's base_mountpoint() then "resolved" to a host
# directory that was never actually created by any real zfs command — so the
# very first attempt to run an image needing ANY native substitute (e.g. plain
# esphome/esphome, whose python3 alone triggers a pkg:python311 provider) failed
# at `mount_nullfs: /var/jailrun/bases: No such file or directory`.
# ---------------------------------------------------------------------------

try:
    from store.store import Store as _Store  # type: ignore[import-not-found]
    store = _Store()
except ImportError:  # running on Linux / seam not yet on PYTHONPATH

    # _MockStore: stand-in for S3 store.Store; satisfies the Store API signature without touching ZFS
    class _MockStore:
        """Stand-in for S3 store.Store. Satisfies the Store API signature."""

        # register_base:start
        #   purpose: compute a content-addressed snapshot ID from the provision command without touching ZFS
        #   input:
        #     name: str — human label, e.g. "jailrun-native-<hash>"
        #     provision: str — a POSIX shell command (see plan_to_provision_cmd())
        #   output:
        #     snapshot_id: str — ZFS snapshot name of the form "zroot/jailrun/bases/<sha256[:16]>"
        #   sideEffects: logs INFO via logging.info("[MOCK store] register_base(...)")
        #   rationale: mock exists so bakery is testable without a live FreeBSD+ZFS host; real store.Store replaces this
        def register_base(self, name: str, provision: str) -> str:
            snap_id = "zroot/jailrun/bases/" + hashlib.sha256(provision.encode()).hexdigest()[:16]
            log.info("[MOCK store] register_base(%s) -> %s", name, snap_id)
            return snap_id
        # register_base:end

    store = _MockStore()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Provider → artifact resolution tables
# ---------------------------------------------------------------------------

# pkg:<name>  →  (artifact_path_in_base, description)
# Paths are under /usr/local (FreeBSD PREFIX) on freebsd-host.
PKG_ARTIFACTS: dict[str, str] = {
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
    # Build-tool deps (crosstool-NG, port builds)
    "bison":          "/usr/local/bin/bison",
    "automake":       "/usr/local/bin/automake",
    "autoconf":       "/usr/local/bin/autoconf",
    "libtool":        "/usr/local/bin/libtool",
    "texinfo":        "/usr/local/bin/makeinfo",   # texinfo installs makeinfo
    "help2man":       "/usr/local/bin/help2man",
    "gperf":          "/usr/local/bin/gperf",
    "zip":            "/usr/local/bin/zip",
    "m4":             "/usr/local/bin/gm4",        # GNU m4 on FreeBSD
}

# Packages that install MANY distinctly-named binaries rather than one binary
# matching the package name (PKG_ARTIFACTS/resolve_pkg's single-path model
# doesn't fit these) — fill_artifact_paths() uses the ORIGINAL binary's own
# basename for these instead. Confirmed live 2026-07-19:
# binutils installs ar/ld/nm/objcopy/readelf/strip/... under /usr/local/bin/,
# not a single "/usr/local/bin/binutils".
MULTI_BINARY_PKGS: frozenset[str] = frozenset({"binutils"})

# port:<origin>  →  artifact_path
# The xtensa-esp-elf port (GCC 13, ESP-IDF 5.2+/5.3+) installs under a
# prefix of /usr/local/xtensa-esp-elf/ (the binaries land in that bin/).
# Source: freshports.org/devel/xtensa-esp-elf (maintained leres@FreeBSD.org,
#         last updated 2026-06-04, version 13.2.0.20240530_17).
PORT_ARTIFACTS: dict[str, str] = {
    "devel/xtensa-esp-elf": "/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-gcc",
    # Legacy flavored variant (pre-5.3 ESP-IDF):
    "devel/xtensa-esp32-elf": "/usr/local/xtensa-esp32-elf-idf52/bin/xtensa-esp32-elf-gcc",
    # esp-quick-toolchain (trombik): ESP8266/lx106 via GCC 10 port
    # Port origin: devel/esp-quick-toolchain
    # Installs to /usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/
    "devel/esp-quick-toolchain": (
        "/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-gcc"
    ),
}

# build:<recipe-id>  →  BuildRecipe

# BuildRecipe: describes one from-source toolchain build — artifact path, pkg/port deps, readiness status
@dataclass
class BuildRecipe:
    recipe_id: str
    description: str
    # Artifact path that exists after the build completes on freebsd-host
    artifact_path: str
    # pkg dependencies that must be installed before the build
    pkg_deps: list[str] = field(default_factory=list)
    # port dependencies to build first
    port_deps: list[str] = field(default_factory=list)
    # status hint
    status: str = "experimental"  # "ready" | "experimental" | "stub"
    notes: str = ""


# Registry of known build recipes.
# crosstool-NG on FreeBSD is "experimental" per upstream docs:
#   https://crosstool-ng.github.io/docs/os-setup/
# The lx106 fork is jcmvbkbc/crosstool-NG (Xtensa-aware branch).
RECIPE_REGISTRY: dict[str, BuildRecipe] = {

    # -----------------------------------------------------------------------
    # xtensa-lx106-elf  —  ESP8266 toolchain
    # No official FreeBSD port exists in ports tree (as of 2026-06).
    # Options:
    #   A) devel/esp-quick-toolchain (trombik, GCC 10, last release 2022-01)
    #   B) Build via jcmvbkbc/crosstool-NG lx106 branch (experimental on BSD)
    # We use esp-quick-toolchain as the primary path because:
    #   - It is a packaged FreeBSD port (even if unofficial)
    #   - crosstool-NG FreeBSD support is explicitly "experimental"
    # -----------------------------------------------------------------------
    "xtensa-lx106-elf": BuildRecipe(
        recipe_id="xtensa-lx106-elf",
        description=(
            "ESP8266 cross-compiler (xtensa-lx106-elf). "
            "Prefers devel/esp-quick-toolchain (trombik unofficial port, GCC 10). "
            "Fallback: build via jcmvbkbc/crosstool-NG lx106 branch."
        ),
        # Primary path via esp-quick-toolchain port
        artifact_path=(
            "/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-gcc"
        ),
        pkg_deps=[
            "gmake", "gawk", "gsed", "bison", "automake", "libtool",
            "texinfo", "help2man", "git",
        ],
        port_deps=["devel/esp-quick-toolchain"],
        status="experimental",
        notes=(
            "devel/esp-quick-toolchain last released 2022-01-29; may need "
            "manual port fetch. Crosstool-NG fallback: clone "
            "jcmvbkbc/crosstool-NG lx106 branch, install FreeBSD deps "
            "(archivers/zip devel/gmake devel/gperf textproc/gsed etc.), "
            "then: ./bootstrap && ./configure "
            "--with-sed=/usr/local/bin/gsed "
            "--with-make=/usr/local/bin/gmake "
            "--with-patch=/usr/local/bin/gpatch "
            "&& gmake && gmake install && ./ct-ng xtensa-lx106-elf "
            "&& ./ct-ng build. "
            "ESP-IDF version for ESP8266 is RTOS SDK v3.x, not mainline IDF."
        ),
    ),

    # -----------------------------------------------------------------------
    # riscv32-esp-elf  —  ESP32-C3/C6/P4 RISC-V toolchain
    # No FreeBSD port exists in tree or as unofficial port (as of 2026-06).
    # The riscv-gnu-toolchain exists at devel/riscv-gnu-toolchain but is
    # for generic RISC-V, not Espressif's specific multilib configuration.
    # Long-tail: only practical path today is Linuxulator + Espressif binary.
    # -----------------------------------------------------------------------
    "riscv32-esp-elf": BuildRecipe(
        recipe_id="riscv32-esp-elf",
        description=(
            "ESP32-C3/C6/P4 RISC-V cross-compiler (riscv32-esp-elf). "
            "STUB — no FreeBSD port exists. "
            "Fallback today: run Linux binary under Linuxulator."
        ),
        artifact_path="/usr/local/riscv32-esp-elf/bin/riscv32-esp-elf-gcc",
        pkg_deps=[
            "gmake", "gawk", "gsed", "bison", "automake", "libtool",
            "texinfo", "help2man", "git", "python311",
        ],
        port_deps=[],
        status="stub",
        notes=(
            "No native FreeBSD binary or port for riscv32-esp-elf. "
            "devel/riscv-gnu-toolchain is generic RISC-V (rv64), not "
            "Espressif's rv32imac multilib variant. "
            "Build path: clone riscv-gnu-toolchain, configure for "
            "rv32imc-ilp32 with Espressif newlib patches — untested on BSD. "
            "Recommended fallback: keep status=linuxulator and run "
            "Espressif's Linux binary under Linuxulator."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

# ProvisionStep: one atomic step in a provisioning plan; kind in {"pkg","port","build"}
@dataclass
class ProvisionStep:
    """One atomic step in a provisioning plan."""
    kind: str          # "pkg" | "port" | "build"
    target: str        # package name, port origin, or recipe-id
    artifact_path: str
    notes: str = ""


# ProvisioningPlan:start
#   purpose: ordered, deduplicated set of pkg/port/build steps derived from a manifest; consumers serialize via as_dict()
#   intent: enforces execution order (pkg -> port -> build) and deduplication across multiple binaries sharing the same dep
#   key invariants:
#     - _pkg_seen/_port_seen/_build_seen prevent duplicate steps
#     - steps order: all pkg steps precede all port steps, port steps precede build steps (flat within kind)
@dataclass
class ProvisioningPlan:
    """
    Ordered set of pkg installs / port builds / source builds needed to
    satisfy all native binaries in one manifest.  Ordered: pkg first, then
    ports, then builds (each category topologically flat — parallel ok within
    kind).
    """
    steps: list[ProvisionStep] = field(default_factory=list)
    # Accumulated pkg names to deduplicate across steps
    _pkg_seen: set[str] = field(default_factory=set, repr=False)
    _port_seen: set[str] = field(default_factory=set, repr=False)
    _build_seen: set[str] = field(default_factory=set, repr=False)

    # add_pkg: appends a pkg step if name not yet seen; mutates self.steps and self._pkg_seen
    def add_pkg(self, name: str, artifact_path: str) -> None:
        if name not in self._pkg_seen:
            self._pkg_seen.add(name)
            self.steps.append(ProvisionStep("pkg", name, artifact_path))

    # add_port: appends a port step if origin not yet seen; mutates self.steps and self._port_seen
    def add_port(self, origin: str, artifact_path: str, notes: str = "") -> None:
        if origin not in self._port_seen:
            self._port_seen.add(origin)
            self.steps.append(ProvisionStep("port", origin, artifact_path, notes))

    # add_build: appends a build step if recipe_id not yet seen; mutates self.steps and self._build_seen
    def add_build(self, recipe_id: str, artifact_path: str, notes: str = "") -> None:
        if recipe_id not in self._build_seen:
            self._build_seen.add(recipe_id)
            self.steps.append(ProvisionStep("build", recipe_id, artifact_path, notes))

    # as_dict: returns JSON-serialisable dict of all steps (pure, no IO)
    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": [
                {"kind": s.kind, "target": s.target,
                 "artifact_path": s.artifact_path, "notes": s.notes}
                for s in self.steps
            ]
        }

    # plan_hash: returns first 16 hex chars of SHA-256 over sorted JSON of as_dict() (pure, no IO)
    def plan_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True).encode()
        ).hexdigest()[:16]
# ProvisioningPlan:end


# ---------------------------------------------------------------------------
# Provider parsing
# ---------------------------------------------------------------------------

_PROVIDER_RE = re.compile(r'^(pkg|port|build):(.+)$')


# parse_provider:start
#   purpose: split a provider string into (kind, target) using _PROVIDER_RE
#   input:
#     provider: str — raw provider string from manifest, e.g. "pkg:cmake" or "build:xtensa-lx106-elf"
#   output:
#     tuple[str, str] — (kind, target) where kind in {"pkg", "port", "build"}
#   sideEffects: none
def parse_provider(provider: str) -> tuple[str, str]:
    """
    Parse a provider string.

    Returns (kind, target) where kind in {"pkg", "port", "build"}.
    Raises ValueError on malformed input.
    """
    m = _PROVIDER_RE.match(provider)
    if not m:
        raise ValueError(
            f"Malformed provider {provider!r}. "
            "Expected 'pkg:<name>', 'port:<origin>', or 'build:<recipe-id>'."
        )
    return m.group(1), m.group(2)
# parse_provider:end


# ---------------------------------------------------------------------------
# Artifact resolution
# ---------------------------------------------------------------------------

# resolve_pkg:start
#   purpose: map a pkg name to its installed binary path on freebsd-host, with heuristic fallback
#   input:
#     name: str — package name from provider string, e.g. "cmake"
#   output:
#     artifact_path: str — absolute path under /usr/local/bin/ or /usr/local/<name>/ where the binary lives
#   sideEffects: logs WARNING via logging.warning() when name is not in PKG_ARTIFACTS table
def resolve_pkg(name: str) -> str:
    """
    Resolve pkg:<name> to an artifact path.

    Falls back to a heuristic /usr/local/bin/<name> if not in the table,
    with a warning (probe S2 may have proposed a name we don't know yet).
    """
    if name in PKG_ARTIFACTS:
        return PKG_ARTIFACTS[name]
    # Heuristic: most pkg-installed CLI tools land in /usr/local/bin/
    guessed = f"/usr/local/bin/{name}"
    log.warning(
        "pkg:%s not in PKG_ARTIFACTS table; guessing %s. "
        "Add to PKG_ARTIFACTS when confirmed on freebsd-host.",
        name, guessed,
    )
    return guessed
# resolve_pkg:end


# resolve_port:start
#   purpose: map a port origin to its installed binary path; no heuristic fallback (port prefixes are non-standard)
#   input:
#     origin: str — port origin string, e.g. "devel/xtensa-esp-elf"
#   output:
#     artifact_path: str — absolute path to the port's installed binary
#   sideEffects: none
def resolve_port(origin: str) -> str:
    """
    Resolve port:<origin> to an artifact path.

    Port installs are not in /usr/local/bin — they use port-specific prefixes.
    """
    if origin in PORT_ARTIFACTS:
        return PORT_ARTIFACTS[origin]
    raise KeyError(
        f"port:{origin} has no entry in PORT_ARTIFACTS. "
        "Add the installed binary path after verifying on freebsd-host."
    )
# resolve_port:end


# resolve_build:start
#   purpose: look up a BuildRecipe by recipe_id; raises KeyError if unknown
#   input:
#     recipe_id: str — recipe identifier, e.g. "xtensa-lx106-elf"
#   output:
#     recipe: BuildRecipe — full recipe including artifact_path, pkg_deps, port_deps, notes
#   sideEffects: none
def resolve_build(recipe_id: str) -> BuildRecipe:
    """Resolve build:<recipe-id> to a BuildRecipe."""
    if recipe_id in RECIPE_REGISTRY:
        return RECIPE_REGISTRY[recipe_id]
    raise KeyError(
        f"build:{recipe_id} has no recipe in RECIPE_REGISTRY. "
        "Add a BuildRecipe entry to cover this toolchain."
    )
# resolve_build:end


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------

# build_plan:start
#   purpose: walk manifest binaries and assemble an ordered ProvisioningPlan with warnings for unresolvable entries
#   input:
#     manifest: dict[str, Any] — parsed Substitution Manifest; must have "binaries" list with status/native/provider fields
#   output:
#     plan: ProvisioningPlan — ordered steps (pkg first, then port, then build)
#     warnings: list[str] — non-fatal resolution failures (missing provider, unknown port/recipe)
#   sideEffects: logs WARNING via logging.warning() for unknown pkg names (via resolve_pkg)
def build_plan(manifest: dict[str, Any]) -> tuple[ProvisioningPlan, list[str]]:
    """
    Walk manifest binaries, resolve each native provider to a plan step.

    Returns (plan, warnings).  plan.steps is in execution order:
      1. pkg installs  (parallel-safe within kind)
      2. port builds   (sequential; ports may have inter-deps)
      3. source builds (sequential; may depend on ports above)
    """
    plan = ProvisioningPlan()
    warnings: list[str] = []

    binaries = manifest.get("binaries", [])

    # START_BINARY_LOOP
    for binary in binaries:
        status = binary.get("status")
        native = binary.get("native")
        path = binary.get("path", "<unknown>")

        if status != "native":
            continue
        if not native:
            # Not a gap: probe() also marks already-native FreeBSD/script
            # binaries as status="native" with no "native" block at all,
            # since there's nothing to substitute (mirrors engine.py's
            # _assemble_native_shadow, which treats this the same way).
            # Nothing to plan for these — just skip, no warning.
            continue
        provider = native.get("provider")
        if not provider:
            warnings.append(f"{path}: native.provider missing — skipping.")
            continue

        try:
            kind, target = parse_provider(provider)
        except ValueError as exc:
            warnings.append(str(exc))
            continue

        # START_RESOLVE_PROVIDER
        if kind == "pkg":
            artifact = resolve_pkg(target)
            plan.add_pkg(target, artifact)

        elif kind == "port":
            try:
                artifact = resolve_port(target)
                plan.add_port(target, artifact)
            except KeyError as exc:
                warnings.append(str(exc))

        elif kind == "build":
            try:
                recipe = resolve_build(target)
                # First, ensure the recipe's pkg deps are in the plan
                for dep_pkg in recipe.pkg_deps:
                    dep_artifact = resolve_pkg(dep_pkg)
                    plan.add_pkg(dep_pkg, dep_artifact)
                # Then port deps
                for dep_port in recipe.port_deps:
                    try:
                        dep_artifact = resolve_port(dep_port)
                        plan.add_port(dep_port, dep_artifact)
                    except KeyError as exc:
                        warnings.append(str(exc))
                # Then the build itself
                plan.add_build(recipe.recipe_id, recipe.artifact_path, recipe.notes)
            except KeyError as exc:
                warnings.append(str(exc))
        # END_RESOLVE_PROVIDER
    # END_BINARY_LOOP

    return plan, warnings
# build_plan:end


# ---------------------------------------------------------------------------
# Plan -> shell command (fixes the register_base contract mismatch —
# store.store.Store.register_base() expects a shell-command STR,
# not the ProvisioningPlan dict this module used to pass it)
# ---------------------------------------------------------------------------

# plan_to_provision_cmd:start
#   purpose: render a ProvisioningPlan's pkg/port steps as a single POSIX shell
#            command suitable for store.register_base()'s provision_cmd
#   input:
#     plan: ProvisioningPlan — resolved plan from build_plan()
#   output:
#     provision_cmd: str — a `sh -c`-safe multi-line shell script
#   sideEffects: none (pure string construction)
#   rationale: pkg names/port origins in a plan are only ever drawn from this
#              module's own PKG_ARTIFACTS/PORT_ARTIFACTS/PROVIDER_MAP tables —
#              probe.propose_native() only proposes a provider when the binary's
#              basename matches a hardcoded table entry, so nothing here
#              originates from untrusted OCI-layer content — but shlex.quote is
#              applied anyway (defense in depth, and correctness for names with
#              shell-meaningful characters). BATCH=yes on port builds avoids an
#              interactive prompt hanging the provisioning subprocess (see
#              engine.py's provisioning timeout tiers — this has a real but finite
#              timeout now).
#   raises: ValueError if the plan contains any build: step — from-source
#           recipes (crosstool-NG etc., see bakery/recipes/*.md) are host-specific
#           multi-step procedures that need human review before running
#           unattended as root; silently skipping or half-running one is worse
#           than refusing outright.
def plan_to_provision_cmd(plan: ProvisioningPlan) -> str:
    build_targets = [s.target for s in plan.steps if s.kind == "build"]
    if build_targets:
        raise ValueError(
            "plan contains build: steps (" + ", ".join(build_targets) + ") — these "
            "need a human-reviewed recipe (see bakery/recipes/*.md), not an "
            "auto-rendered shell command; provision manually, or extend "
            "plan_to_provision_cmd() once a recipe is scripted and reviewed."
        )

    # JAILRUN_BASE_ROOT is set by store.py's register_base() call sites to the
    # base dataset's own mountpoint. Without -r/DESTDIR, `pkg install` and
    # `make install` both target the LIVE HOST's real system (cwd alone does not
    # redirect them) — confirmed live 2026-07-19: the very
    # first manifest needing any pkg install would otherwise have silently
    # installed onto the FreeBSD dev host itself instead of the isolated base.
    lines = ["set -eu", ': "${JAILRUN_BASE_ROOT:?JAILRUN_BASE_ROOT must be set by the caller}"']
    pkg_names = [s.target for s in plan.steps if s.kind == "pkg"]
    if pkg_names:
        # A fresh base dataset has no repo catalog cache of its own yet — `pkg -r`
        # isolates the whole package DB (not just installed files) under
        # JAILRUN_BASE_ROOT, so it needs its own `update` before the first
        # `install` (confirmed live 2026-07-19: "Repository ... cannot be opened.
        # 'pkg update' required").
        lines.append('pkg -r "$JAILRUN_BASE_ROOT" update')
        lines.append(
            'pkg -r "$JAILRUN_BASE_ROOT" install -y ' + " ".join(shlex.quote(n) for n in pkg_names)
        )
    for step in plan.steps:
        if step.kind == "port":
            lines.append(
                f'make -C /usr/ports/{shlex.quote(step.target)} install clean BATCH=yes '
                'DESTDIR="$JAILRUN_BASE_ROOT"'
            )
    return "\n".join(lines)
# plan_to_provision_cmd:end


# ---------------------------------------------------------------------------
# Manifest update — fill artifact_path fields
# ---------------------------------------------------------------------------

# fill_artifact_paths:start
#   purpose: produce a deep-copy of the manifest with native.artifact_path set for every step in plan,
#            bumping native.verification "guessed" -> "exists" when the resolved path is confirmed
#            present on disk
#   input:
#     manifest: dict[str, Any] — original Substitution Manifest (not mutated)
#     plan: ProvisioningPlan — resolved plan whose steps carry (kind, target) -> artifact_path mapping
#   output:
#     updated: dict[str, Any] — deep copy of manifest with native.artifact_path filled, status
#              confirmed as "native", and native.verification bumped to "exists" wherever the
#              filled artifact_path was confirmed present on disk (left as "guessed" otherwise —
#              e.g. resolve_pkg's heuristic "guessing ..." fallback path, or a resolved path that
#              simply isn't there yet)
#   sideEffects: logs WARNING via logging.warning() for binaries whose provider has no resolved
#                artifact in plan; stats each filled artifact_path via Path.exists() (read-only)
#   rationale: this is L0->L1 of the verification ladder only (schemas/substitution-manifest.schema.json)
#              — bakery never invents "runs"/"behaves"/"proven" (L2-L4); those come from a future
#              agent verification harness that actually exercises the binary.
def fill_artifact_paths(
    manifest: dict[str, Any],
    plan: ProvisioningPlan,
) -> dict[str, Any]:
    """
    Return a copy of manifest with native.artifact_path filled for every
    resolved binary, status confirmed as 'native', and native.verification
    bumped 'guessed' -> 'exists' when the filled path is confirmed present
    on disk.
    """
    # START_BUILD_RESOLVED_INDEX
    # Build a lookup: (kind, target) → artifact_path from the plan
    resolved: dict[tuple[str, str], str] = {}
    for step in plan.steps:
        resolved[(step.kind, step.target)] = step.artifact_path
    # END_BUILD_RESOLVED_INDEX

    # START_FILL_MANIFEST_COPY
    updated = copy.deepcopy(manifest)
    for binary in updated.get("binaries", []):
        if binary.get("status") != "native":
            continue
        native = binary.get("native")
        if not native or not native.get("provider"):
            continue
        try:
            kind, target = parse_provider(native["provider"])
        except ValueError:
            continue
        key = (kind, target)
        if key in resolved:
            if kind == "pkg" and target in MULTI_BINARY_PKGS:
                # A package like binutils installs MANY distinct tools
                # (ar/ld/nm/objcopy/...), not one binary matching the package
                # name — PKG_ARTIFACTS/resolve_pkg's single-path-per-package
                # model doesn't fit. Confirmed live 2026-07-19:
                # every binutils-provided binary was pointed at the nonexistent
                # /usr/local/bin/binutils and correctly skipped by engine.py's
                # artifact-reality check — use the ORIGINAL binary's own
                # basename instead, since FreeBSD ports for GNU-compatible
                # tools install under the same name as their Linux counterparts.
                native["artifact_path"] = f"/usr/local/bin/{Path(binary['path']).name}"
            else:
                native["artifact_path"] = resolved[key]
            binary["status"] = "native"  # confirm

            # START_BUMP_VERIFICATION
            # L0 ("guessed", stamped by probe(S2)) -> L1 ("exists") only when
            # the filled path is actually confirmed present. resolve_pkg()'s
            # heuristic "guessing ..." fallback (name not in PKG_ARTIFACTS)
            # produces a path we have never confirmed, so it — same as any
            # resolved path that just isn't there — leaves verification at
            # "guessed". Never invent L2-L4 (runs/behaves/proven) here.
            if Path(native["artifact_path"]).exists():
                native["verification"] = "exists"
            # END_BUMP_VERIFICATION
        else:
            log.warning(
                "No resolved artifact for %s provider=%s",
                binary.get("path"), native["provider"],
            )
    # END_FILL_MANIFEST_COPY

    return updated
# fill_artifact_paths:end


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# bake:start
#   purpose: main entry point — resolve all native providers, register the base with S3, return the annotated manifest
#   input:
#     manifest: dict[str, Any] — parsed Substitution Manifest from S2 probe
#   output:
#     updated: dict[str, Any] — manifest copy with native.artifact_path filled and "_bakery" metadata block appended
#   sideEffects:
#     - calls store.register_base(base_name, plan_dict) — in production triggers ZFS base provisioning on freebsd-host via S3 store
#     - logs INFO via logging.info() at start, after registration, and on completion
#     - logs WARNING via logging.warning() for each unresolvable provider entry
def bake(manifest: dict[str, Any]) -> dict[str, Any]:
    """
    Main entry point for S4 bakery.

    Args:
        manifest: a parsed Substitution Manifest dict (from probe S2).

    Returns:
        updated manifest with native.artifact_path filled for all resolved
        binaries and a registered base snapshot via store.register_base().

    Side-effects:
        Calls store.register_base(name, provision) — in production this
        triggers the ZFS base provisioning on freebsd-host.
    """
    image = manifest.get("image", "unknown")
    log.info("bakery.bake: processing manifest for %s", image)

    # START_BUILD_AND_WARN
    plan, warnings = build_plan(manifest)
    for w in warnings:
        log.warning("bakery: %s", w)
    # END_BUILD_AND_WARN

    if not plan.steps:
        log.info("bakery: no native providers to resolve for %s", image)
        return manifest

    # START_REGISTER_BASE
    # Register the base with S3. plan_to_provision_cmd() renders a shell command
    # (store.register_base() shells out via `sh -c` — a dict here would TypeError,
    # see plan_to_provision_cmd's docstring). A plan containing a
    # build: step can't be auto-rendered; degrade gracefully (fill what resolved,
    # skip base registration, surface it as a warning) rather than crashing bake().
    plan_dict = plan.as_dict()
    base_name = f"jailrun-native-{plan.plan_hash()}"
    try:
        provision_cmd = plan_to_provision_cmd(plan)
    except ValueError as exc:
        warnings = warnings + [str(exc)]
        log.warning("bakery: %s", exc)
        updated = fill_artifact_paths(manifest, plan)
        updated["_bakery"] = {
            "base_name": base_name,
            "snapshot_id": None,
            "plan": plan_dict,
            "warnings": warnings,
        }
        return updated

    snapshot_id = store.register_base(base_name, provision_cmd)
    log.info("bakery: registered base %s -> %s", base_name, snapshot_id)
    # END_REGISTER_BASE

    # START_ANNOTATE_MANIFEST
    # Fill artifact_path in manifest copy
    updated = fill_artifact_paths(manifest, plan)

    # Annotate the manifest with the base snapshot for runtime S1
    updated["_bakery"] = {
        "base_name": base_name,
        "snapshot_id": snapshot_id,
        "plan": plan_dict,
        "warnings": warnings,
    }
    # END_ANNOTATE_MANIFEST

    log.info(
        "bakery.bake: done — %d plan steps, %d warnings",
        len(plan.steps), len(warnings),
    )
    return updated
# bake:end


# ---------------------------------------------------------------------------
# CLI helper (freebsd-host validation shim — not the production path)
# ---------------------------------------------------------------------------

# _cli:start
#   purpose: argparse CLI shim for manual validation on freebsd-host; supports --plan-only for dry-run and --output for file write
#   input:
#     sys.argv — manifest path (positional), --plan-only flag, --output/-o path
#   output:
#     none (writes JSON to stdout or file)
#   sideEffects:
#     - reads manifest file via open(args.manifest)
#     - writes updated manifest JSON to stdout (print) or to file via Path(args.output).write_text()
#     - configures logging to stderr via logging.basicConfig()
#     - calls bake() which in turn calls store.register_base() unless --plan-only is set
def _cli() -> None:
    import sys
    import argparse

    parser = argparse.ArgumentParser(
        description="bakery: resolve native providers in a substitution manifest"
    )
    parser.add_argument("manifest", help="path to substitution manifest JSON")
    parser.add_argument(
        "--plan-only", action="store_true",
        help="print the provisioning plan and exit without registering",
    )
    parser.add_argument(
        "--output", "-o", default="-",
        help="write updated manifest to file (default: stdout)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    # START_CLI_LOAD_MANIFEST
    with open(args.manifest) as fh:
        manifest = json.load(fh)
    # END_CLI_LOAD_MANIFEST

    # START_CLI_DISPATCH
    if args.plan_only:
        plan, warnings = build_plan(manifest)
        for w in warnings:
            log.warning(w)
        print(json.dumps(plan.as_dict(), indent=2))
        return

    updated = bake(manifest)

    out = json.dumps(updated, indent=2)
    if args.output == "-":
        print(out)
    else:
        Path(args.output).write_text(out)
        log.info("Wrote updated manifest to %s", args.output)
    # END_CLI_DISPATCH
# _cli:end

if __name__ == "__main__":
    _cli()

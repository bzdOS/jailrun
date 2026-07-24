# START_AI_HEADER
# MODULE: providers/backfill.py
# PURPOSE: standalone registry-verification backfill — walks every entry in
#          providers/provider-map.json, resolves each Linux binary's FreeBSD
#          artifact path (reusing bakery's EXACT resolution), checks whether
#          that path exists on the current host, and writes the per-binary
#          verification level + evidence record into providers/verification.json.
# INTENT: roadmap 0.3 — "Backfill: every mapping that ships today (all currently
#         guessed) driven to >=L1." probe() stamps verification="guessed" (L0) and
#         bakery.fill_artifact_paths() bumps to "exists" (L1) only at a specific
#         IMAGE's manifest-build time; until this tool, nothing walked
#         provider-map.json ITSELF and persisted verification levels into a static
#         file independent of any one image run. This is that tool. L1 here uses
#         bakery's own definition verbatim ("artifact path confirmed present on a
#         real FreeBSD host" = Path.exists()); L2-L4 (runs/behaves/proven) come
#         from the agent verification harness (roadmap 0.3/0.8), NEVER from here.
# DEPENDENCIES: stdlib (json, os, platform, subprocess, datetime, pathlib);
#               bakery (parse_provider/resolve_pkg/resolve_port/resolve_build);
#               providers (PROVIDER_MAP/PKG_ARTIFACTS/PORT_ARTIFACTS/MULTI_BINARY_PKGS)
# PUBLIC_API: resolve_artifact(basename, provider) -> (path|None, source);
#             collect(provider_map=PROVIDER_MAP, exists=os.path.exists) -> dict;
#             render(collected, host_info) -> dict; host_info() -> dict; main()
# END_AI_HEADER
"""
providers/backfill.py — standalone L0->L1 verification backfill for the registry.

The host-existence check is INJECTED (``exists`` kwarg, default ``os.path.exists``)
so this module is fully unit-testable on Linux with a fake checker — no FreeBSD
host required for the tests. The real evidence is produced by running this on (or
proxying the existence checks to) a FreeBSD host; the resulting
providers/verification.json is the shipped, machine-checkable record that every
provider-map.json entry's artifact path has been confirmed on a real FreeBSD host.

Run for real on a FreeBSD host (or see the deploy notes for a proxied run):

    python3 -m providers.backfill            # writes providers/verification.json
    python3 -m providers.backfill --check    # print summary, write nothing
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from providers import (
    MULTI_BINARY_PKGS,
    MULTI_BINARY_SUFFIX,
    PKG_ARTIFACTS,
    PORT_ARTIFACTS,
    PROVIDER_MAP,
)
from bakery.bakery import (
    parse_provider,
    resolve_build,
    resolve_pkg,
    resolve_port,
)

# providers/ is this module's own directory — locate verification.json next to
# the other registry data files regardless of the process cwd (mirrors
# providers/__init__.py's _DATA_DIR reasoning).
_DATA_DIR = Path(__file__).parent


# resolve_artifact:start
#   purpose: resolve one provider-map.json entry's FreeBSD artifact path, reusing
#            bakery's resolution tables/logic verbatim so this tool and bakery
#            never disagree on where a binary lives
#   input:
#     basename: str — the Linux binary basename (a provider-map.json key)
#     provider: str — the provider string (pkg:<name> | port:<origin> | build:<id>)
#   output:
#     (artifact_path: str | None, source: str) — artifact_path is the absolute path
#             bakery would fill into a manifest for this binary, or None if no path
#             can be resolved (e.g. a port: origin absent from PORT_ARTIFACTS);
#             source names WHERE the path came from: "pkg-artifacts" / "heuristic"
#             / "multi-binary-pkg" / "port-artifacts" / "unresolved-port" /
#             "build-recipe" / "unresolved-build" — provenance the evidence record
#             carries so a reviewer can tell a table-confirmed path from a guess.
#   sideEffects: may log via bakery.resolve_pkg's logging.warning on a heuristic
#                pkg name not in PKG_ARTIFACTS (the same warning a real bake emits)
def resolve_artifact(basename: str, provider: str) -> tuple[str | None, str]:
    kind, target = parse_provider(provider)
    if kind == "pkg":
        # Multi-binary packages (binutils, gcc16, llvm22) install many tools
        # under their OWN names, not one binary named after the package — mirror
        # bakery.fill_artifact_paths() exactly: use the Linux binary's basename.
        # gcc16/llvm22 install ONLY versioned names (gcc16/clang22) with no
        # unversioned symlink, so append MULTI_BINARY_SUFFIX's per-pkg version.
        if target in MULTI_BINARY_PKGS:
            suffix = MULTI_BINARY_SUFFIX.get(target, "")
            return f"/usr/local/bin/{basename}{suffix}", "multi-binary-pkg"
        path = resolve_pkg(target)  # PKG_ARTIFACTS[table] or heuristic guess
        source = "pkg-artifacts" if target in PKG_ARTIFACTS else "heuristic"
        return path, source
    if kind == "port":
        try:
            return resolve_port(target), "port-artifacts"
        except KeyError:
            return None, "unresolved-port"
    if kind == "build":
        try:
            return resolve_build(target).artifact_path, "build-recipe"
        except KeyError:
            return None, "unresolved-build"
    # parse_provider rejects anything else, so this is unreachable.
    return None, "unresolved-unknown"
# resolve_artifact:end


# collect:start
#   purpose: walk a provider map, resolve + existence-check every entry, return a
#            per-basename evidence record (L0->L1 of the verification ladder only)
#   input:
#     provider_map: dict[str, str] — basename -> provider string (default: the
#                   shipped providers.PROVIDER_MAP)
#     exists: Callable[[str], bool] — host path-existence check, default
#             os.path.exists; INJECTED so unit tests feed a fake without FreeBSD
#   output:
#     collected: dict[str, dict] — basename -> {provider, artifact_path,
#                verification, evidence:{method, result, source}}. verification is
#                "exists" (L1) only when a resolved path is confirmed present;
#                otherwise "guessed" (L0) with evidence.result "absent" (path
#                resolved but not on this host) or "unresolved" (no path at all,
#                e.g. an unmapped port). Never returns runs/behaves/proven (L2-L4).
#   sideEffects: none (pure; the existence callable is the only host touch)
def collect(
    provider_map: dict[str, str] | None = None,
    exists: Callable[[str], bool] = os.path.exists,
) -> dict[str, dict[str, Any]]:
    provider_map = PROVIDER_MAP if provider_map is None else provider_map
    out: dict[str, dict[str, Any]] = {}
    for basename, provider in sorted(provider_map.items()):
        artifact, source = resolve_artifact(basename, provider)
        if artifact is None:
            verification, result = "guessed", "unresolved"
        elif exists(artifact):
            verification, result = "exists", "present"
        else:
            verification, result = "guessed", "absent"
        out[basename] = {
            "provider": provider,
            "artifact_path": artifact,
            "verification": verification,
            "evidence": {"method": "path-exists", "result": result, "source": source},
        }
    return out
# collect:end


# host_info:start
#   purpose: gather the host context that makes an evidence record reproducible
#   output:
#     info: dict — {host, freebsd_version, checked_at, method}; freebsd_version
#           comes from `freebsd-version -u` when available (the canonical kernel/
#           userland version string on FreeBSD), falling back to platform.release()
#           off FreeBSD so the field is never empty
#   sideEffects: runs `freebsd-version -u` via subprocess (read-only, best-effort)
def host_info() -> dict[str, str]:
    fv = ""
    try:
        fv = subprocess.run(
            ["freebsd-version", "-u"], capture_output=True, text=True, timeout=5,
            check=False,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        fv = ""
    if not fv:
        fv = platform.release() or "unknown"
    return {
        "host": platform.node(),
        "freebsd_version": fv,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "path-exists",
    }
# host_info:end


# render:start
#   purpose: assemble the providers/verification.json document from collected
#            evidence + host context
def render(collected: dict[str, dict[str, Any]], info: dict[str, str]) -> dict[str, Any]:
    return {"schema_version": 1, "generated": info, "entries": collected}
# render:end


def _summary(collected: dict[str, dict[str, Any]]) -> str:
    n = len(collected)
    exists_n = sum(1 for e in collected.values() if e["verification"] == "exists")
    unresolved = sum(
        1 for e in collected.values() if e["evidence"]["result"] == "unresolved"
    )
    return (
        f"{n} entries: {exists_n} at L1 (exists), "
        f"{n - exists_n - unresolved} at L0 (absent), {unresolved} unresolved"
    )


def main(argv: list[str] | None = None) -> int:
    check_only = bool(argv and "--check" in argv)
    collected = collect()
    info = host_info()
    print(_summary(collected))
    if check_only:
        return 0
    doc = render(collected, info)
    out = _DATA_DIR / "verification.json"
    out.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

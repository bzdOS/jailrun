#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: providers/test_backfill.py
# PURPOSE: unit tests for providers/backfill.py — the standalone L0->L1 registry
#          verification backfill (roadmap 0.3). The host-existence check is
#          INJECTED into collect(), so these run on Linux with a fake checker and
#          need no FreeBSD host; they pin the resolution reuse (bakery's tables +
#          multi-binary handling), the L1 ceiling (never invents runs/behaves/
#          proven), and that no shipped provider-map.json entry is silently dropped.
# INTENT: guard the "Backfill: every mapping driven to >=L1" exit criterion — a
#         regression here would either mis-resolve a binary's artifact path
#         (diverging from bakery, which would then claim a path the registry says
#         is unconfirmed) or silently skip entries, hiding gaps.
# DEPENDENCIES: stdlib (os, sys), providers (PROVIDER_MAP), providers.backfill
# PUBLIC_API: pytest test_* functions
# END_AI_HEADER
"""Unit tests for providers/backfill.py. Run anywhere (no FreeBSD needed):

    python3 -m pytest providers/test_backfill.py -v
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from providers import PROVIDER_MAP  # noqa: E402
from providers.backfill import (  # noqa: E402
    collect,
    host_info,
    render,
    resolve_artifact,
)


def test_resolve_pkg_from_table():
    path, src = resolve_artifact("python3", "pkg:python311")
    assert path == "/usr/local/bin/python3.11"
    assert src == "pkg-artifacts"


def test_resolve_multi_binary_pkg_uses_basename():
    # binutils is the shipped multi-binary pkg: ld/ar/nm/... -> /usr/local/bin/<basename>
    for bn in ("ld", "ar", "nm", "readelf", "strip", "objcopy"):
        provider = PROVIDER_MAP[bn]
        path, src = resolve_artifact(bn, provider)
        assert path == f"/usr/local/bin/{bn}", bn
        assert src == "multi-binary-pkg", bn


def test_resolve_heuristic_pkg_not_in_table():
    # A pkg name absent from PKG_ARTIFACTS -> bakery's heuristic
    # /usr/local/bin/<target>, flagged as a guess (not table). Synthetic name so
    # the test does not depend on which real pkgs happen to be table-listed.
    path, src = resolve_artifact("sometool", "pkg:sometool")
    assert path == "/usr/local/bin/sometool"
    assert src == "heuristic"


def test_resolve_multi_binary_versioned_pkg_appends_suffix():
    # gcc16/llvm22 install ONLY versioned binaries (gcc16, g++16, clang22,
    # clang++22) with NO unversioned symlink — the per-pkg MULTI_BINARY_SUFFIX
    # is appended to the basename so the resolved path matches the real binary.
    path, src = resolve_artifact("gcc", "pkg:gcc16")
    assert path == "/usr/local/bin/gcc16"
    assert src == "multi-binary-pkg"
    assert resolve_artifact("g++", "pkg:gcc16")[0] == "/usr/local/bin/g++16"
    assert resolve_artifact("clang", "pkg:llvm22")[0] == "/usr/local/bin/clang22"
    assert resolve_artifact("clang++", "pkg:llvm22")[0] == "/usr/local/bin/clang++22"


def test_resolve_port_from_table():
    path, src = resolve_artifact("xtensa-esp32-elf-gcc", "port:devel/xtensa-esp-elf")
    assert path.endswith("xtensa-esp-elf-gcc")
    assert src == "port-artifacts"


def test_resolve_unresolved_port_returns_none():
    path, src = resolve_artifact("foo", "port:nonexistent/origin")
    assert path is None
    assert src == "unresolved-port"


def test_collect_present_bumps_to_exists():
    pm = {"x": "pkg:somepkg"}  # somepkg not in PKG_ARTIFACTS -> heuristic path
    present = {"/usr/local/bin/somepkg"}
    out = collect(pm, exists=lambda p: p in present)
    e = out["x"]
    assert e["verification"] == "exists"
    assert e["evidence"]["result"] == "present"
    assert e["artifact_path"] == "/usr/local/bin/somepkg"


def test_collect_absent_stays_guessed():
    out = collect({"x": "pkg:somepkg"}, exists=lambda p: False)
    assert out["x"]["verification"] == "guessed"
    assert out["x"]["evidence"]["result"] == "absent"


def test_collect_unresolved_port_is_not_checked():
    # exists() must not even be consulted when no path can be resolved.
    calls = []

    def spying_exists(p):
        calls.append(p)
        return True

    out = collect({"foo": "port:nonexistent/origin"}, exists=spying_exists)
    assert out["foo"]["verification"] == "guessed"
    assert out["foo"]["evidence"]["result"] == "unresolved"
    assert out["foo"]["artifact_path"] is None
    assert calls == [], "exists must not be called for an unresolved entry"


def test_collect_never_invents_l2_l4():
    # Even with exists() always True, verification tops out at "exists" (L1).
    out = collect({"x": "pkg:somepkg"}, exists=lambda p: True)
    assert out["x"]["verification"] == "exists"


def test_collect_covers_every_shipped_entry():
    # The real run must not silently drop any shipped provider-map.json entry.
    out = collect(PROVIDER_MAP, exists=lambda p: False)
    assert set(out) == set(PROVIDER_MAP)
    for e in out.values():
        assert e["verification"] in ("guessed", "exists")
        assert e["evidence"]["result"] in ("present", "absent", "unresolved")
        assert set(e) == {"provider", "artifact_path", "verification", "evidence"}
        assert set(e["evidence"]) == {"method", "result", "source"}


def test_render_shape():
    collected = collect({"x": "pkg:somepkg"}, exists=lambda p: False)
    info = {"host": "h", "freebsd_version": "15.1", "checked_at": "t", "method": "path-exists"}
    doc = render(collected, info)
    assert doc["schema_version"] == 1
    assert doc["generated"] is info
    assert set(doc) == {"schema_version", "generated", "entries"}
    assert "x" in doc["entries"]


def test_host_info_has_required_fields():
    info = host_info()
    for k in ("host", "freebsd_version", "checked_at", "method"):
        assert k in info and info[k], k


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

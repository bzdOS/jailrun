#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: bakery/test_plan_to_provision_cmd.py
# PURPOSE: unit tests for build_plan()/plan_to_provision_cmd() — pure functions, no
#          subprocess, no real Store — covers what validate_manifest_schema.py (CI)
#          deliberately does NOT touch since bake() now calls the real Store
# INTENT: bake() was fixed 2026-07-19 to call the real
#         store.store.Store instead of an always-mocked stand-in; that means CI
#         (a Linux runner) can no longer safely call bake() on a manifest that
#         needs actual provisioning. These tests cover the pure planning/rendering
#         logic directly instead, so the pkg/port rendering path still has real
#         coverage without needing FreeBSD tools.
# DEPENDENCIES: stdlib (sys, os), bakery.bakery (build_plan, plan_to_provision_cmd)
# PUBLIC_API: run_all, TESTS; each test_* is also callable directly by pytest
# END_AI_HEADER
"""
test_plan_to_provision_cmd.py — pure unit tests for bakery's plan/render logic.

Run on host (no FreeBSD required):
    python3 -m pytest bakery/test_plan_to_provision_cmd.py -v
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bakery.bakery import build_plan, plan_to_provision_cmd  # noqa: E402


def _manifest(binaries):
    return {"image": "test:latest", "binaries": binaries, "linuxulator": {"required": False}}


def test_pkg_only_plan_renders_pkg_install():
    manifest = _manifest([
        {"path": "/usr/bin/python3", "status": "native",
         "native": {"provider": "pkg:python311", "artifact_path": None}},
    ])
    plan, warnings = build_plan(manifest)
    assert warnings == []
    cmd = plan_to_provision_cmd(plan)
    lines = cmd.splitlines()
    assert lines[0] == "set -eu"
    assert "JAILRUN_BASE_ROOT" in lines[1]  # guard line
    assert lines[2] == 'pkg -r "$JAILRUN_BASE_ROOT" update'
    assert lines[3] == 'pkg -r "$JAILRUN_BASE_ROOT" install -y python311'


def test_pkg_and_port_plan_renders_both_in_order():
    manifest = _manifest([
        {"path": "/usr/bin/python3", "status": "native",
         "native": {"provider": "pkg:python311", "artifact_path": None}},
        {"path": "/root/.platformio/xtensa-esp32-elf-gcc", "status": "native",
         "native": {"provider": "port:devel/xtensa-esp-elf", "artifact_path": None}},
    ])
    plan, warnings = build_plan(manifest)
    assert warnings == []
    cmd = plan_to_provision_cmd(plan)
    lines = cmd.splitlines()
    assert lines[0] == "set -eu"
    assert lines[2] == 'pkg -r "$JAILRUN_BASE_ROOT" update'
    assert lines[3] == 'pkg -r "$JAILRUN_BASE_ROOT" install -y python311'
    assert lines[4] == (
        'make -C /usr/ports/devel/xtensa-esp-elf install clean BATCH=yes '
        'DESTDIR="$JAILRUN_BASE_ROOT"'
    )


def test_pkg_names_are_shell_quoted():
    manifest = _manifest([
        {"path": "/usr/bin/weird", "status": "native",
         "native": {"provider": "pkg:weird;name", "artifact_path": None}},
    ])
    plan, _warnings = build_plan(manifest)
    cmd = plan_to_provision_cmd(plan)
    assert "'weird;name'" in cmd  # shlex.quote wraps shell-meaningful chars


def test_build_step_refuses_to_render():
    manifest = _manifest([
        {"path": "/usr/bin/xtensa-lx106-elf-gcc", "status": "native",
         "native": {"provider": "build:xtensa-lx106-elf", "artifact_path": None}},
    ])
    plan, _warnings = build_plan(manifest)
    try:
        plan_to_provision_cmd(plan)
        raise AssertionError("expected ValueError for a build: step")
    except ValueError as exc:
        assert "xtensa-lx106-elf" in str(exc)


def test_empty_plan_renders_just_the_preamble():
    manifest = _manifest([])
    plan, warnings = build_plan(manifest)
    assert warnings == []
    assert plan.steps == []
    lines = plan_to_provision_cmd(plan).splitlines()
    assert lines[0] == "set -eu"
    assert "JAILRUN_BASE_ROOT" in lines[1]
    assert len(lines) == 2  # nothing to install -> just the two guard lines


TESTS = [
    test_pkg_only_plan_renders_pkg_install,
    test_pkg_and_port_plan_renders_both_in_order,
    test_pkg_names_are_shell_quoted,
    test_build_step_refuses_to_render,
    test_empty_plan_renders_just_the_preamble,
]


def run_all():
    for t in TESTS:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    run_all()

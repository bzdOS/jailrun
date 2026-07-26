# START_AI_HEADER
# MODULE: runtime/doctor.py
# PURPOSE: inspect a host and report jailrun readiness; provide exact fixes on failure
# INTENT: `jailrun doctor` command—detect missing tools, modules, config without crashing on first error
# DEPENDENCIES: stdlib (platform, shutil, subprocess, os, json); never raises on missing tool/config
# PUBLIC_API: CheckResult type; run_checks() -> list[CheckResult]; render(results, fmt) -> str; exit_code_for_results(results) -> int
# END_AI_HEADER

"""
jailrun doctor — host readiness inspector.

Runs nine checks:
   1. os_is_freebsd — platform.system() == 'FreeBSD'
   2. skopeo_present — shutil.which('skopeo') is not None
   3. bsdtar_present — shutil.which('bsdtar') is not None
   4. zpool_present — `zpool list <ZPOOL>` exits 0 (FreeBSD only, skip if not)
   5. racct_enabled — sysctl -n kern.racct.enable == '1' (FreeBSD only)
   6. linux64_loaded — kldstat -q -m linux64 exits 0 (FreeBSD only, report as "info" not fail)
   7. pkg_trust_keys — os.path.isdir('/usr/share/keys/pkg') (FreeBSD only)
   8. devfs_delegation — when nested, whether the outer jail delegates devfs
      ruleset 4 to its children (FreeBSD only, skip if not nested; report as
      "info" not fail — the fix is config on a jail this process doesn't own)
   9. pf_anchor_hooks — /etc/pf.conf contains nat-anchor and anchor for jailrun (FreeBSD only)

Each check returns a CheckResult with: name, status (ok|fail|skip|info), detail, fix.
Never raises—FileNotFoundError, CalledProcessError, OSError all map to "fail" or "skip".
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# CheckResult:start
#   purpose: represent the outcome of a single host readiness check
#   fields:
#     name: str — check identifier (e.g. 'os_is_freebsd')
#     status: Literal['ok', 'fail', 'skip', 'info'] — outcome of the check
#     detail: str — what was observed (e.g. 'platform.system()=Linux' or 'skopeo found at /usr/bin/skopeo')
#     fix: str — remediation text on failure; empty string when status is ok/skip
@dataclass(frozen=True)
class CheckResult:
    """Result of a single readiness check."""
    name: str
    status: Literal["ok", "fail", "skip", "info"]
    detail: str
    fix: str = ""
# CheckResult:end


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------

# _check_os_is_freebsd:start
#   purpose: verify that the host OS is FreeBSD
#   input: none
#   output: CheckResult with status ok/fail and detail describing the OS
#   sideEffects: calls platform.system() only (no I/O)
def _check_os_is_freebsd() -> CheckResult:
    """Check platform.system() == 'FreeBSD'."""
    current_os = platform.system()
    if current_os == "FreeBSD":
        return CheckResult(
            name="os_is_freebsd",
            status="ok",
            detail=f"platform.system()={current_os}",
            fix="",
        )
    return CheckResult(
        name="os_is_freebsd",
        status="fail",
        detail=f"platform.system()={current_os}",
        fix="jailrun's runtime needs FreeBSD; you can edit/test on any OS but runs require a FreeBSD 15+ host.",
    )
# _check_os_is_freebsd:end


# _check_skopeo_present:start
#   purpose: verify that skopeo binary is available in PATH
#   input: none
#   output: CheckResult with status ok/fail; if found, detail is the path
#   sideEffects: calls shutil.which('skopeo')
def _check_skopeo_present() -> CheckResult:
    """Check shutil.which('skopeo') is not None."""
    path = shutil.which("skopeo")
    if path:
        return CheckResult(
            name="skopeo_present",
            status="ok",
            detail=f"found at {path}",
            fix="",
        )
    return CheckResult(
        name="skopeo_present",
        status="fail",
        detail="not found in PATH",
        fix="pkg install skopeo",
    )
# _check_skopeo_present:end


# _check_bsdtar_present:start
#   purpose: verify that bsdtar binary is available in PATH
#   input: none
#   output: CheckResult with status ok/fail; if found, detail is the path
#   sideEffects: calls shutil.which('bsdtar')
def _check_bsdtar_present() -> CheckResult:
    """Check shutil.which('bsdtar') is not None."""
    path = shutil.which("bsdtar")
    if path:
        return CheckResult(
            name="bsdtar_present",
            status="ok",
            detail=f"found at {path}",
            fix="",
        )
    return CheckResult(
        name="bsdtar_present",
        status="fail",
        detail="not found in PATH",
        fix="bsdtar missing — expected in FreeBSD base system; on other OSes install libarchive-tools.",
    )
# _check_bsdtar_present:end


# _check_zpool_present:start
#   purpose: verify that the named ZFS pool is available (FreeBSD only)
#   input: none
#   output: CheckResult with status ok/fail/skip; runs 'zpool list <ZPOOL>' (ZPOOL from env or default 'jailrun')
#   sideEffects: runs subprocess.run(['zpool', 'list', ...], capture_output=True); never raises
def _check_zpool_present() -> CheckResult:
    """Check zpool list <ZPOOL> exits 0 (FreeBSD only)."""
    if platform.system() != "FreeBSD":
        return CheckResult(
            name="zpool_present",
            status="skip",
            detail="skipped (not FreeBSD)",
            fix="",
        )

    zpool = os.environ.get("JAILRUN_ZPOOL", "jailrun")
    try:
        result = subprocess.run(
            ["zpool", "list", zpool],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return CheckResult(
                name="zpool_present",
                status="ok",
                detail=f"pool '{zpool}' is available",
                fix="",
            )
        return CheckResult(
            name="zpool_present",
            status="fail",
            detail=f"zpool list {zpool} failed (exit {result.returncode})",
            fix="create a dedicated pool, e.g.: zpool create jailrun <device> (see docs/DEV_ENVIRONMENT.md).",
        )
    except FileNotFoundError:
        return CheckResult(
            name="zpool_present",
            status="fail",
            detail="zpool command not found",
            fix="create a dedicated pool, e.g.: zpool create jailrun <device> (see docs/DEV_ENVIRONMENT.md).",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="zpool_present",
            status="fail",
            detail="zpool list timed out",
            fix="create a dedicated pool, e.g.: zpool create jailrun <device> (see docs/DEV_ENVIRONMENT.md).",
        )
    except Exception as exc:
        return CheckResult(
            name="zpool_present",
            status="fail",
            detail=f"zpool check failed: {exc}",
            fix="create a dedicated pool, e.g.: zpool create jailrun <device> (see docs/DEV_ENVIRONMENT.md).",
        )
# _check_zpool_present:end


# _check_racct_enabled:start
#   purpose: verify that racct (resource accounting) is enabled via sysctl (FreeBSD only)
#   input: none
#   output: CheckResult with status ok/fail/skip; runs 'sysctl -n kern.racct.enable' and checks output == '1'
#   sideEffects: runs subprocess.run(['sysctl', '-n', 'kern.racct.enable']); never raises
def _check_racct_enabled() -> CheckResult:
    """Check sysctl -n kern.racct.enable output == '1' (FreeBSD only)."""
    if platform.system() != "FreeBSD":
        return CheckResult(
            name="racct_enabled",
            status="skip",
            detail="skipped (not FreeBSD)",
            fix="",
        )

    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.racct.enable"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            if output == "1":
                return CheckResult(
                    name="racct_enabled",
                    status="ok",
                    detail="kern.racct.enable=1",
                    fix="",
                )
            return CheckResult(
                name="racct_enabled",
                status="fail",
                detail=f"kern.racct.enable={output} (need 1)",
                fix="rctl limits need it: add kern.racct.enable=1 to /boot/loader.conf and reboot (loader tunable).",
            )
        return CheckResult(
            name="racct_enabled",
            status="fail",
            detail=f"sysctl failed (exit {result.returncode})",
            fix="rctl limits need it: add kern.racct.enable=1 to /boot/loader.conf and reboot (loader tunable).",
        )
    except FileNotFoundError:
        return CheckResult(
            name="racct_enabled",
            status="fail",
            detail="sysctl command not found",
            fix="rctl limits need it: add kern.racct.enable=1 to /boot/loader.conf and reboot (loader tunable).",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="racct_enabled",
            status="fail",
            detail="sysctl timed out",
            fix="rctl limits need it: add kern.racct.enable=1 to /boot/loader.conf and reboot (loader tunable).",
        )
    except Exception as exc:
        return CheckResult(
            name="racct_enabled",
            status="fail",
            detail=f"racct check failed: {exc}",
            fix="rctl limits need it: add kern.racct.enable=1 to /boot/loader.conf and reboot (loader tunable).",
        )
# _check_racct_enabled:end


# _check_linux64_loaded:start
#   purpose: verify that linux64 kernel module is loaded (FreeBSD only, advisory only)
#   input: none
#   output: CheckResult with status ok/info/skip; runs 'kldstat -q -m linux64' and checks exit code
#   sideEffects: runs subprocess.run(['kldstat', '-q', '-m', 'linux64']); never raises; reported as 'info' not fail
def _check_linux64_loaded() -> CheckResult:
    """Check kldstat -q -m linux64 exits 0 (FreeBSD only, report as "info" not fail)."""
    if platform.system() != "FreeBSD":
        return CheckResult(
            name="linux64_loaded",
            status="skip",
            detail="skipped (not FreeBSD)",
            fix="",
        )

    try:
        result = subprocess.run(
            ["kldstat", "-q", "-m", "linux64"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return CheckResult(
                name="linux64_loaded",
                status="ok",
                detail="linux64 module is loaded",
                fix="",
            )
        return CheckResult(
            name="linux64_loaded",
            status="info",
            detail="linux64 module is not loaded",
            fix="only needed for the Tier-2 Linuxulator fallback; load with: kldload linux64 (or provision-freebsd.sh --tier2). Not required for the native path.",
        )
    except FileNotFoundError:
        return CheckResult(
            name="linux64_loaded",
            status="info",
            detail="kldstat command not found",
            fix="only needed for the Tier-2 Linuxulator fallback; load with: kldload linux64 (or provision-freebsd.sh --tier2). Not required for the native path.",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="linux64_loaded",
            status="info",
            detail="kldstat timed out",
            fix="only needed for the Tier-2 Linuxulator fallback; load with: kldload linux64 (or provision-freebsd.sh --tier2). Not required for the native path.",
        )
    except Exception as exc:
        return CheckResult(
            name="linux64_loaded",
            status="info",
            detail=f"linux64 check failed: {exc}",
            fix="only needed for the Tier-2 Linuxulator fallback; load with: kldload linux64 (or provision-freebsd.sh --tier2). Not required for the native path.",
        )
# _check_linux64_loaded:end


# _check_pkg_trust_keys:start
#   purpose: verify that pkg trust keys directory exists (FreeBSD only)
#   input: none
#   output: CheckResult with status ok/fail/skip; checks os.path.isdir('/usr/share/keys/pkg')
#   sideEffects: calls os.path.isdir() only (no subprocess)
def _check_pkg_trust_keys() -> CheckResult:
    """Check os.path.isdir('/usr/share/keys/pkg') (FreeBSD only)."""
    if platform.system() != "FreeBSD":
        return CheckResult(
            name="pkg_trust_keys",
            status="skip",
            detail="skipped (not FreeBSD)",
            fix="",
        )

    if os.path.isdir("/usr/share/keys/pkg"):
        return CheckResult(
            name="pkg_trust_keys",
            status="ok",
            detail="/usr/share/keys/pkg exists",
            fix="",
        )
    return CheckResult(
        name="pkg_trust_keys",
        status="fail",
        detail="/usr/share/keys/pkg does not exist",
        fix="seed pkg trust keys into fresh bases (see store.py _seed_pkg_trust_keys / provision-freebsd.sh).",
    )
# _check_pkg_trust_keys:end


# _check_pf_anchor_hooks:start
#   purpose: verify that /etc/pf.conf defines jailrun anchor hooks for --network allow isolation
#   input: none
#   output: CheckResult with status ok/fail/skip; reads /etc/pf.conf, looks for 'nat-anchor "jailrun/*"' and 'anchor "jailrun/*"'
#   sideEffects: reads /etc/pf.conf via open(); never raises
def _check_pf_anchor_hooks() -> CheckResult:
    """Check /etc/pf.conf contains nat-anchor and anchor for jailrun (FreeBSD only)."""
    if platform.system() != "FreeBSD":
        return CheckResult(
            name="pf_anchor_hooks",
            status="skip",
            detail="skipped (not FreeBSD)",
            fix="",
        )

    try:
        with open("/etc/pf.conf") as fh:
            content = fh.read()
    except FileNotFoundError:
        return CheckResult(
            name="pf_anchor_hooks",
            status="info",
            detail="/etc/pf.conf not found",
            fix="create /etc/pf.conf with nat-anchor and anchor rules for jailrun (see docs/ADVANCED.md).",
        )
    except PermissionError:
        return CheckResult(
            name="pf_anchor_hooks",
            status="info",
            detail="/etc/pf.conf not readable",
            fix="check file permissions.",
        )
    except Exception as exc:
        return CheckResult(
            name="pf_anchor_hooks",
            status="info",
            detail=f"cannot read /etc/pf.conf: {exc}",
            fix="check file permissions.",
        )

    has_nat = 'nat-anchor "jailrun' in content
    has_anchor = 'anchor "jailrun' in content

    if has_nat and has_anchor:
        return CheckResult(
            name="pf_anchor_hooks",
            status="ok",
            detail="/etc/pf.conf defines nat-anchor and anchor for jailrun",
            fix="",
        )
    missing = []
    if not has_nat:
        missing.append('nat-anchor "jailrun/*"')
    if not has_anchor:
        missing.append('anchor "jailrun/*"')
    return CheckResult(
        name="pf_anchor_hooks",
        status="info",
        detail=f"/etc/pf.conf missing: {', '.join(missing)}",
        fix="add the following to /etc/pf.conf before the scrub/rdr rules:\n"
        '    nat-anchor "jailrun/*"\n'
        '    anchor "jailrun/*"\n'
        "The anchor is auto-populated by jailrun at runtime for --network allow isolation. "
        "Without it, containers with --network allow will have unrestricted network access.",
    )


# _check_pf_anchor_hooks:end


# _check_devfs_delegation:start
#   purpose: when jailrun runs nested (inside another jail), verify that outer
#            jail delegates devfs ruleset 4 to its own children, so nested
#            run-jails get a restricted /dev with no --allow-unrestricted-devfs
#            escape hatch needed
#   input: none
#   output: CheckResult with status ok/info/skip; skip when not FreeBSD or not
#           running nested; info (not fail — same treatment as pf_anchor_hooks,
#           since the operator-facing fix is config on a jail this process does
#           not own) when nested and delegation is missing
#   sideEffects: when nested, runs engine._devfs_delegation_available(), which
#                creates and destroys one throwaway probe jail; never raises
def _check_devfs_delegation() -> CheckResult:
    """Check whether a nested outer jail delegates devfs ruleset 4 (FreeBSD only)."""
    if platform.system() != "FreeBSD":
        return CheckResult(
            name="devfs_delegation",
            status="skip",
            detail="skipped (not FreeBSD)",
            fix="",
        )

    try:
        from runtime.engine import _devfs_delegation_available, _is_jailed  # noqa: PLC0415
    except Exception as exc:
        return CheckResult(
            name="devfs_delegation",
            status="info",
            detail=f"could not check (runtime.engine import failed: {exc})",
            fix="",
        )

    if not _is_jailed():
        return CheckResult(
            name="devfs_delegation",
            status="skip",
            detail="not running nested — jailrun is on the bare host, "
            "mount.devfs already gets ruleset 4 natively",
            fix="",
        )

    if _devfs_delegation_available():
        return CheckResult(
            name="devfs_delegation",
            status="ok",
            detail="outer jail delegates devfs ruleset 4 — nested run-jails "
            "get a restricted /dev with no opt-in needed",
            fix="",
        )

    return CheckResult(
        name="devfs_delegation",
        status="info",
        detail="outer jail does not delegate devfs ruleset 4 to its "
        "children — nested `jailrun run` refuses by default "
        "(or falls back to an unrestricted /dev under "
        "--allow-unrestricted-devfs)",
        fix="recreate the OUTER jail (the one jailrun itself runs inside) "
        "with:\n"
        "    allow.mount;\n"
        "    allow.mount.devfs;\n"
        "    enforce_statfs = 1;\n"
        "    devfs_ruleset = 4;\n"
        "    children.max = <N>;\n"
        "No jailrun-side change is needed once these are set — nested "
        "run-jails will get ruleset 4 automatically. See ROADMAP.md.",
    )


# _check_devfs_delegation:end


# ---------------------------------------------------------------------------
# Main check runner and renderer
# ---------------------------------------------------------------------------

# run_checks:start
#   purpose: execute all nine host readiness checks
#   input: none
#   output: list[CheckResult] — one result per check, in order
#   sideEffects: calls each individual check function; none of them raise; result is always a list of 9 items
def run_checks() -> list[CheckResult]:
    """Run all nine checks; return list of CheckResult."""
    return [
        _check_os_is_freebsd(),
        _check_skopeo_present(),
        _check_bsdtar_present(),
        _check_zpool_present(),
        _check_racct_enabled(),
        _check_linux64_loaded(),
        _check_pkg_trust_keys(),
        _check_devfs_delegation(),
        _check_pf_anchor_hooks(),
    ]
# run_checks:end


# render:start
#   purpose: format a list of CheckResults as text or JSON
#   input:
#     results: list[CheckResult] — output of run_checks()
#     fmt: str — one of 'text' (default) or 'json'
#   output:
#     formatted: str — human-readable or JSON rendering
#   sideEffects: none (pure formatting)
def render(results: list[CheckResult], fmt: str = "text") -> str:
    """
    Format check results as text or JSON.

    Text format: per-check lines with OK/FAIL/SKIP/INFO marker and fix on failure.
    JSON format: json.dumps of the result list.
    """
    if fmt == "json":
        # Convert dataclass to dict for JSON serialization
        data = [
            {
                "name": r.name,
                "status": r.status,
                "detail": r.detail,
                "fix": r.fix,
            }
            for r in results
        ]
        return json.dumps(data, indent=2)

    # Text format
    lines = []
    marker_map = {
        "ok": "[OK]",
        "fail": "[FAIL]",
        "skip": "[SKIP]",
        "info": "[INFO]",
    }
    for r in results:
        marker = marker_map.get(r.status, "[?]")
        lines.append(f"{marker} {r.name}: {r.detail}")
        if r.fix:
            lines.append(f"    Fix: {r.fix}")
    return "\n".join(lines)
# render:end


# exit_code_for_results:start
#   purpose: compute the exit code based on check results
#   input:
#     results: list[CheckResult] — output of run_checks()
#   output:
#     exit_code: int — 0 if no "fail" status present; 1 if any "fail" is present
#   sideEffects: none (pure computation)
def exit_code_for_results(results: list[CheckResult]) -> int:
    """Return 0 if all checks are ok/skip/info, 1 if any check failed."""
    return 1 if any(r.status == "fail" for r in results) else 0
# exit_code_for_results:end

#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_doctor.py
# PURPOSE: unit tests for doctor.py rendering and exit-code logic using synthetic CheckResult lists
# INTENT: verify render() text/json output and exit_code_for_results() against mock data; runnable on any OS
# DEPENDENCIES: stdlib (json); runtime.doctor (CheckResult, render, exit_code_for_results)
# PUBLIC_API: each test_* function is callable by pytest
# END_AI_HEADER

"""
test_doctor.py — unit tests for runtime/doctor.py.

Tests rendering and exit-code logic against synthetic CheckResult lists.
Does NOT test real host state (which varies); instead uses mock data to verify
text/json output shape and exit-code semantics.

Run with pytest:
    python3 -m pytest runtime/test_doctor.py -v
"""

import json
from runtime.doctor import CheckResult, render, exit_code_for_results


# ---------------------------------------------------------------------------
# Test data — synthetic CheckResults
# ---------------------------------------------------------------------------

# Mock a fully passing host
RESULTS_ALL_OK = [
    CheckResult(name="os_is_freebsd", status="ok", detail="platform.system()=FreeBSD", fix=""),
    CheckResult(name="skopeo_present", status="ok", detail="found at /usr/bin/skopeo", fix=""),
    CheckResult(name="bsdtar_present", status="ok", detail="found at /usr/bin/tar", fix=""),
    CheckResult(name="zpool_present", status="ok", detail="pool 'jailrun' is available", fix=""),
    CheckResult(name="racct_enabled", status="ok", detail="kern.racct.enable=1", fix=""),
    CheckResult(name="linux64_loaded", status="ok", detail="linux64 module is loaded", fix=""),
    CheckResult(name="pkg_trust_keys", status="ok", detail="/usr/share/keys/pkg exists", fix=""),
]

# Mock a Linux host (skips FreeBSD-specific checks)
RESULTS_LINUX_HOST = [
    CheckResult(name="os_is_freebsd", status="fail", detail="platform.system()=Linux", fix="jailrun's runtime needs FreeBSD; you can edit/test on any OS but runs require a FreeBSD 15+ host."),
    CheckResult(name="skopeo_present", status="ok", detail="found at /usr/bin/skopeo", fix=""),
    CheckResult(name="bsdtar_present", status="fail", detail="not found in PATH", fix="bsdtar missing — expected in FreeBSD base system; on other OSes install libarchive-tools."),
    CheckResult(name="zpool_present", status="skip", detail="skipped (not FreeBSD)", fix=""),
    CheckResult(name="racct_enabled", status="skip", detail="skipped (not FreeBSD)", fix=""),
    CheckResult(name="linux64_loaded", status="skip", detail="skipped (not FreeBSD)", fix=""),
    CheckResult(name="pkg_trust_keys", status="skip", detail="skipped (not FreeBSD)", fix=""),
]

# Mock missing zpool
RESULTS_NO_ZPOOL = [
    CheckResult(name="os_is_freebsd", status="ok", detail="platform.system()=FreeBSD", fix=""),
    CheckResult(name="skopeo_present", status="ok", detail="found at /usr/bin/skopeo", fix=""),
    CheckResult(name="bsdtar_present", status="ok", detail="found at /usr/bin/tar", fix=""),
    CheckResult(name="zpool_present", status="fail", detail="zpool list jailrun failed (exit 1)", fix="create a dedicated pool, e.g.: zpool create jailrun <device> (see docs/DEV_ENVIRONMENT.md)."),
    CheckResult(name="racct_enabled", status="ok", detail="kern.racct.enable=1", fix=""),
    CheckResult(name="linux64_loaded", status="info", detail="linux64 module is not loaded", fix="only needed for the Tier-2 Linuxulator fallback; load with: kldload linux64 (or provision-freebsd.sh --tier2). Not required for the native path."),
    CheckResult(name="pkg_trust_keys", status="ok", detail="/usr/share/keys/pkg exists", fix=""),
]

# Mock with some skipped (non-FreeBSD) and one info (linux64)
RESULTS_MIXED_STATUS = [
    CheckResult(name="os_is_freebsd", status="ok", detail="platform.system()=FreeBSD", fix=""),
    CheckResult(name="skopeo_present", status="ok", detail="found at /usr/bin/skopeo", fix=""),
    CheckResult(name="bsdtar_present", status="skip", detail="skipped (not FreeBSD)", fix=""),
    CheckResult(name="zpool_present", status="ok", detail="pool 'jailrun' is available", fix=""),
    CheckResult(name="racct_enabled", status="info", detail="some advisory info", fix="this is informational only"),
    CheckResult(name="linux64_loaded", status="info", detail="another advisory", fix="also just info"),
    CheckResult(name="pkg_trust_keys", status="ok", detail="/usr/share/keys/pkg exists", fix=""),
]


# ---------------------------------------------------------------------------
# Text rendering tests
# ---------------------------------------------------------------------------

# CONTRACT: text render of RESULTS_ALL_OK contains [OK] markers for all checks + no fix text
def test_render_text_all_ok():
    """Text render of all-ok results contains [OK] markers, no fix lines."""
    output = render(RESULTS_ALL_OK, fmt="text")
    assert "[OK]" in output
    assert "[FAIL]" not in output
    assert "[SKIP]" not in output
    for result in RESULTS_ALL_OK:
        assert result.name in output
    # No fix lines should appear when all are ok
    assert "Fix:" not in output
    print("PASS test_render_text_all_ok")


# CONTRACT: text render of RESULTS_LINUX_HOST contains [FAIL] markers for os_is_freebsd + bsdtar, [SKIP] for freebsd-only checks + fix text on failures
def test_render_text_linux_host():
    """Text render of Linux host results shows failures and skips."""
    output = render(RESULTS_LINUX_HOST, fmt="text")
    # Check for status markers
    assert "[FAIL]" in output
    assert "[OK]" in output
    assert "[SKIP]" in output
    # Check for specific checks
    assert "os_is_freebsd" in output
    assert "bsdtar_present" in output
    assert "zpool_present" in output
    # Check for fix text on failures
    assert "FreeBSD 15+ host" in output
    assert "libarchive-tools" in output
    print("PASS test_render_text_linux_host")


# CONTRACT: text render of RESULTS_NO_ZPOOL shows [FAIL] for zpool + fix text + [OK] for others
def test_render_text_zpool_failure():
    """Text render of zpool failure shows the fix for zpool + other statuses."""
    output = render(RESULTS_NO_ZPOOL, fmt="text")
    assert "[FAIL]" in output
    assert "[OK]" in output
    assert "[INFO]" in output
    # zpool failure should have fix text
    assert "zpool_present" in output
    assert "zpool create jailrun" in output
    # info status should also be shown
    assert "linux64_loaded" in output
    print("PASS test_render_text_zpool_failure")


# CONTRACT: text render of RESULTS_MIXED_STATUS contains all four status markers [OK] [SKIP] [INFO] [FAIL] if present, or just [OK] [SKIP] [INFO]
def test_render_text_mixed_status():
    """Text render shows all status types present in the result set."""
    output = render(RESULTS_MIXED_STATUS, fmt="text")
    assert "[OK]" in output
    assert "[SKIP]" in output
    assert "[INFO]" in output
    # No fails in this set
    assert "[FAIL]" not in output
    # Info items should have fix text
    assert "racct_enabled" in output
    assert "this is informational only" in output
    print("PASS test_render_text_mixed_status")


# ---------------------------------------------------------------------------
# JSON rendering tests
# ---------------------------------------------------------------------------

# CONTRACT: json render produces valid JSON with array of objects, each with name/status/detail/fix keys
def test_render_json_structure():
    """JSON render is valid JSON with correct structure."""
    output = render(RESULTS_ALL_OK, fmt="json")
    data = json.loads(output)  # Will raise if invalid JSON
    assert isinstance(data, list)
    assert len(data) == len(RESULTS_ALL_OK)
    for item in data:
        assert "name" in item
        assert "status" in item
        assert "detail" in item
        assert "fix" in item
    print("PASS test_render_json_structure")


# CONTRACT: json render of RESULTS_LINUX_HOST contains failures and skips with correct fix texts
def test_render_json_content():
    """JSON render contains correct status and fix texts."""
    output = render(RESULTS_LINUX_HOST, fmt="json")
    data = json.loads(output)
    # Find the os_is_freebsd check
    os_check = next(d for d in data if d["name"] == "os_is_freebsd")
    assert os_check["status"] == "fail"
    assert "FreeBSD 15+" in os_check["fix"]
    # Find a skip check
    zpool_check = next(d for d in data if d["name"] == "zpool_present")
    assert zpool_check["status"] == "skip"
    assert zpool_check["fix"] == ""  # skip should have no fix
    print("PASS test_render_json_content")


# CONTRACT: json render with empty fix string produces valid JSON with fix as empty string
def test_render_json_empty_fix():
    """JSON render preserves empty fix strings."""
    output = render(RESULTS_ALL_OK, fmt="json")
    data = json.loads(output)
    for item in data:
        assert item["fix"] == ""
    print("PASS test_render_json_empty_fix")


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------

# CONTRACT: exit_code_for_results(RESULTS_ALL_OK) returns 0
def test_exit_code_all_ok():
    """Exit code is 0 when all checks pass."""
    code = exit_code_for_results(RESULTS_ALL_OK)
    assert code == 0, f"expected exit code 0, got {code}"
    print("PASS test_exit_code_all_ok")


# CONTRACT: exit_code_for_results(RESULTS_LINUX_HOST) returns 1 (contains failures)
def test_exit_code_with_failure():
    """Exit code is 1 when any check fails."""
    code = exit_code_for_results(RESULTS_LINUX_HOST)
    assert code == 1, f"expected exit code 1, got {code}"
    print("PASS test_exit_code_with_failure")


# CONTRACT: exit_code_for_results(RESULTS_NO_ZPOOL) returns 1 (zpool failure)
def test_exit_code_zpool_failure():
    """Exit code is 1 for zpool failure even with other ok checks."""
    code = exit_code_for_results(RESULTS_NO_ZPOOL)
    assert code == 1, f"expected exit code 1, got {code}"
    print("PASS test_exit_code_zpool_failure")


# CONTRACT: exit_code_for_results(RESULTS_MIXED_STATUS) returns 0 (no fails, only ok/skip/info)
def test_exit_code_mixed_no_fail():
    """Exit code is 0 when there are no failures (ok/skip/info are OK)."""
    code = exit_code_for_results(RESULTS_MIXED_STATUS)
    assert code == 0, f"expected exit code 0, got {code}"
    print("PASS test_exit_code_mixed_no_fail")


# CONTRACT: exit_code_for_results([]) returns 0 (empty result list = no failures)
def test_exit_code_empty_results():
    """Exit code is 0 for empty result list."""
    code = exit_code_for_results([])
    assert code == 0, f"expected exit code 0, got {code}"
    print("PASS test_exit_code_empty_results")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

TESTS = [
    test_render_text_all_ok,
    test_render_text_linux_host,
    test_render_text_zpool_failure,
    test_render_text_mixed_status,
    test_render_json_structure,
    test_render_json_content,
    test_render_json_empty_fix,
    test_exit_code_all_ok,
    test_exit_code_with_failure,
    test_exit_code_zpool_failure,
    test_exit_code_mixed_no_fail,
    test_exit_code_empty_results,
]


# run_all:start
#   purpose: execute every function in TESTS, collect failures, report pass/fail counts
#   input: none
#   output: none (results printed to stdout)
#   sideEffects: prints PASS/FAIL/ERROR lines per test; calls sys.exit(1) if any failure
def run_all():
    import sys  # noqa: PLC0415
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

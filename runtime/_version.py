"""jailrun version detection — git describe, fallback, platform info."""

import subprocess
import sys
import os
from pathlib import Path

_JAILRUN_DIR = Path(__file__).resolve().parent.parent

_GIT_DESCRIBE_CACHE: str | None = None

def _git_describe() -> str | None:
    global _GIT_DESCRIBE_CACHE
    if _GIT_DESCRIBE_CACHE is not None:
        return _GIT_DESCRIBE_CACHE
    try:
        r = subprocess.run(
            ["git", "-C", str(_JAILRUN_DIR), "describe", "--tags", "--dirty=.dirty"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            _GIT_DESCRIBE_CACHE = r.stdout.strip()
            return _GIT_DESCRIBE_CACHE
    except Exception:
        pass
    return None


def _version_file() -> str | None:
    p = _JAILRUN_DIR / "VERSION"
    if p.exists():
        return p.read_text().strip()
    return None


def jailrun_version() -> str:
    v = _git_describe() or _version_file() or "1.0.0a1"
    return v


def platform_info() -> str:
    try:
        import subprocess
        r = subprocess.run(["uname", "-sr"], capture_output=True, text=True, timeout=5)
        os_str = r.stdout.strip()
    except Exception:
        os_str = sys.platform
    py_ver = sys.version.split()[0]
    return f"{os_str}  Python {py_ver}"


def version_banner() -> str:
    return (
        f"jailrun version {jailrun_version()}\n"
        f"platform: {platform_info()}\n"
        f"runtime: FreeBSD jails + ZFS (freebsd-host only)\n"
        f"host build: linux-host/Linux (development)"
    )

#!/usr/bin/env python3
"""Build a self-contained jailrun.pyz zipapp for distribution.

Usage:
    python3 scripts/build_zipapp.py

Output: dist/jailrun.pyz  (single-file, self-contained)
"""
import subprocess, sys, os, shutil, zipapp
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
MAIN = "runtime.cli:main"

def build_zipapp():
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / "jailrun.pyz"

    # Clean previous
    if out.exists():
        out.unlink()

    # Create zipapp — copies the repo packages into a zip archive
    # with a __main__.py that runs runtime.cli.main()
    # We use setuptools' zipapp support: the entry point declaration in
    # pyproject.toml tells pip/setuptools which module to invoke.
    #
    # For the raw zipapp approach, we write a __main__.py wrapper.

    tmp = DIST / "_build"
    if tmp.exists():
        shutil.rmtree(tmp)

    # Determine version
    v = "0.7.0"
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "describe", "--tags", "--dirty=.dirty"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            v = r.stdout.strip()
    except Exception:
        pass

    print(f"Building jailrun zipapp version {v} ...")

    # Write __main__.py that calls runtime.cli.main
    main_py = tmp / "__main__.py"
    main_py.parent.mkdir(parents=True, exist_ok=True)
    main_py.write_text(
        '"""jailrun zipapp entry point."""\n'
        'import sys\n'
        'from runtime.cli import main\n'
        'sys.exit(main())\n'
    )

    # Write VERSION file
    (tmp / "VERSION").write_text(v)

    # Copy packages: runtime, probe, store, bakery, providers, schemas
    for pkg in ["runtime", "probe", "store", "bakery", "providers", "schemas"]:
        src = REPO / pkg
        dst = tmp / pkg
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
            print(f"  copied {pkg}/")

    # Create the zipapp
    with open(out, "wb") as f:
        zipapp.create_archive(
            tmp,
            target=f,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )

    # Clean temp
    shutil.rmtree(tmp)

    size = out.stat().st_size
    print(f"\nDone: {out} ({size / 1024:.0f} KiB)")
    return str(out)


if __name__ == "__main__":
    build_zipapp()

"""
runtime/runner.py — background worker for detached jailrun jails.

Called by `jailrun run -d` after writing a JSON job file.  The runner:

  1. Loads the job file (image_ref, cmd, opts).
  2. Calls engine.run() — the full lifecycle: resolve → clone → manifest →
     shadow → jail -c → jexec → teardown.
  3. Deletes the job file.
  4. Exits with the jailed command's exit code.

The caller (`jailrun` CLI with `-d`) has already printed the jail/container
name and returned to the shell before this script even starts.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python3 -m runtime.runner <job-file>", file=sys.stderr)
        return 1

    job_path: str = sys.argv[1]

    try:
        with open(job_path) as fh:
            job = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"runner: cannot load job {job_path!r}: {exc}", file=sys.stderr)
        return 1

    image_ref: str = job["image_ref"]
    cmd: list[str] = job["cmd"]
    opts: dict = job["opts"]

    # Import here (not at module level) so the module is importable on any
    # host for test purposes — only the actual run path needs the real engine.
    from runtime.engine import run  # noqa: PLC0415

    try:
        rc = run(image_ref, cmd, opts)
    except Exception as exc:
        print(f"runner: {exc}", file=sys.stderr)
        rc = 1
    finally:
        try:
            os.unlink(job_path)
        except OSError:
            pass

    return rc


if __name__ == "__main__":
    sys.exit(main())

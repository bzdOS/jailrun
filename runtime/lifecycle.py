# START_AI_HEADER
# MODULE: runtime/lifecycle.py
# PURPOSE: native jail process lifecycle (SIGSTOP/SIGCONT/SIGKILL via jexec)
# INTENT: self-contained — no external daemon; uses FreeBSD jail(8) + kill(1) directly
# DEPENDENCIES: stdlib (asyncio, logging, subprocess); FreeBSD base (jexec, jls, kill)
# PUBLIC_API: Lifecycled (class), teardown(jail_name) (async), NotAvailable
# END_AI_HEADER
"""
jailrun.runtime.lifecycle — native jail process lifecycle.

Self-contained: no external daemon, no IPC socket. Uses FreeBSD base
commands directly:

  FREEZE    jexec <jail> kill -STOP -1   (pause all jail processes, 0% CPU)
  THAW      jexec <jail> kill -CONT -1   (resume, instant)
  KILL      jexec <jail> kill -KILL -1   (SIGKILL all jail processes)
  STATUS    jls -n                        (list running jails)

HIBERNATE / RESTORE are not yet implemented (need ZFS snapshot integration
with the engine's dataset tracking); they raise NotAvailable, honestly.

Responsibility split (see ARCHITECTURE.md):
  jailrun   OWNS create / mount / jexec / ZFS (store + engine).
  this mod  OWNS process-level signals (STOP/CONT/KILL) inside a running jail.

How jexec kill works:
  jexec <jail> kill -<SIG> -1
  - jexec attaches to the jail's namespace and runs kill(1).
  - kill -1 (pid -1) sends the signal to all processes the caller can see
    and has permission to signal — inside a jail that's exactly that jail's
    processes (minus the transient kill process itself).
  - The kill process exits immediately; the signal is already delivered.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

log = logging.getLogger("jailrun.lifecycle")

_TIMEOUT_S = 10.0


class NotAvailable(RuntimeError):
    """Raised when a lifecycle op is not supported on this platform."""


def _jail_signal(jail: str, sig: str) -> dict:
    """Send a signal to all processes in a jail via ``jexec <jail> kill -<sig> -1``."""
    try:
        r = subprocess.run(
            ["jexec", jail, "kill", f"-{sig}", "-1"],
            capture_output=True,
            timeout=_TIMEOUT_S,
        )
        if r.returncode == 0:
            return {"ok": True, "msg": f"{sig} delivered to {jail}"}
        err = r.stderr.decode(errors="replace").strip()
        return {"ok": False, "err": err or f"jexec kill -{sig} -1 failed (rc={r.returncode})"}
    except FileNotFoundError:
        return {"ok": False, "err": "jexec not found — not running on FreeBSD?"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"jexec kill -{sig} -1 timed out ({_TIMEOUT_S}s)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "err": str(exc)}


class Lifecycled:
    """Native jail lifecycle — uses FreeBSD jexec/jls directly."""

    def __init__(self, sock_path: str = "") -> None:
        # sock_path retained for API compatibility; unused (no daemon).
        self.sock_path = sock_path

    def available(self) -> bool:
        """True if jexec is present (i.e. we're on FreeBSD)."""
        try:
            subprocess.run(
                ["jexec", "-h"],
                capture_output=True, timeout=2,
            )
            return True
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001
            return True  # best-effort: jexec exists but -h isn't supported

    def freeze(self, jail: str) -> dict:
        """SIGSTOP all processes in the jail (pause, 0% CPU, state in RAM)."""
        return _jail_signal(jail, "STOP")

    def thaw(self, jail: str) -> dict:
        """SIGCONT all frozen jail processes (instant resume)."""
        return _jail_signal(jail, "CONT")

    def kill(self, jail: str) -> dict:
        """SIGKILL all processes in the jail."""
        return _jail_signal(jail, "KILL")

    def hibernate(self, jail: str) -> dict:  # noqa: ARG002
        raise NotAvailable(
            "hibernate requires ZFS snapshot integration — not yet wired into lifecycle.py"
        )

    def restore(self, jail: str) -> dict:  # noqa: ARG002
        raise NotAvailable(
            "restore requires ZFS snapshot integration — not yet wired into lifecycle.py"
        )

    def status(self) -> dict:
        """List all running jails via jls."""
        try:
            r = subprocess.run(
                ["jls", "-n"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                blocks = [b.strip() for b in r.stdout.strip().split("\n\n") if b.strip()]
                return {"ok": True, "jails": blocks}
            return {"ok": False, "err": r.stderr.strip()}
        except FileNotFoundError:
            return {"ok": False, "err": "jls not found — not running on FreeBSD?"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "err": str(exc)}


async def teardown(jail_name: str, *, sock_path: str = "") -> None:
    """
    Best-effort SIGKILL all jail processes via native jexec, before the
    engine follows with ``jail -r`` (removes the persist jail) and
    ``store.destroy`` (ZFS destroy + unmount).

    Non-fatal: if jexec fails (jail already gone, not on FreeBSD, etc.)
    the engine's ``jail -r`` handles teardown regardless.
    """
    try:
        res = await asyncio.to_thread(_jail_signal, jail_name, "KILL")
        if res.get("ok"):
            log.info("native KILL %s -> ok", jail_name)
        else:
            log.debug("native KILL %s non-fatal: %s", jail_name, res.get("err", ""))
    except Exception as exc:  # noqa: BLE001
        log.debug("native KILL %s failed (non-fatal): %s", jail_name, exc)

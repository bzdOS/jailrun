# START_AI_HEADER
# MODULE: runtime/lifecycle.py
# PURPOSE: synchronous client for bsdos_lifecycled (bzdOS jail lifecycle daemon)
# INTENT: jailrun delegates jail process-lifecycle (FREEZE/THAW/HIBERNATE/RESTORE/KILL/STATUS)
#         to bsdos_lifecycled rather than hand-rolling signal delivery; this module owns
#         the AF_UNIX IPC seam to that daemon; the jail/ZFS lifecycle remains in engine.py
# DEPENDENCIES: stdlib (asyncio, json, logging, os, socket); external: bsdos_lifecycled
#               Unix socket at /var/run/bsdos-lifecycle.sock (bzdOS artefact)
# PUBLIC_API: Lifecycled (class), teardown(jail_name) (async)
# END_AI_HEADER
"""
jailrun.runtime.lifecycle — client for **bsdos_lifecycled** (the bzdOS jail
lifecycle daemon), reused as jailrun's jail-process-lifecycle backend.

Responsibility split (see ARCHITECTURE.md):
  jailrun        OWNS create / mount / jexec / ZFS (store + engine).
  bsdos_lifecycled OWNS the *running jail's process lifecycle*. We delegate to it
                 instead of hand-rolling signalling/teardown, reusing proven code
                 (jail_get(2) + sysctl(KERN_PROC_PROC) PID-targeted signals).

Wire protocol (from bsdOS/lifecycled/src/main.rs, read 2026-06-30):
  transport : AF_UNIX stream socket, default /var/run/bsdos-lifecycle.sock
  request   : one line  "<VERB> <jail_name>\\n"
              VERB ∈ FREEZE | THAW | HIBERNATE | RESTORE | KILL | STATUS
  response  : a line — JSON like {"cmd":..,"ok":bool,"msg"/"err":..}; STATUS
              returns {"ok":true,"jails":[{app_id,jid,state,pids}]}
  jail_name : == the jail's name. jailrun names its jails "jailrun-<handle>".

Semantics (bsdos_lifecycled):
  FREEZE    SIGSTOP all jail procs   (0% CPU, state stays in RAM)
  THAW      SIGCONT                  (instant resume, <1ms)
  HIBERNATE ZFS snapshot + SIGSTOP   (RAM-light; FreeBSD has no RAM swap-out)
  RESTORE   thaw, or restore from snapshot
  KILL      SIGKILL all procs + bsdOS cleanup (the process half of `--rm`)
  STATUS    list tracked jails

Fallback (daemon absent — not installed / not FreeBSD / socket missing):
  KILL degrades to nothing here (the engine always follows with `jail -r`, which
  removes the jail and its procs). FREEZE/THAW/HIBERNATE/RESTORE are
  lifecycled-ONLY features — without the daemon they raise NotAvailable, honestly.

DEPENDENCY: bsdos_lifecycled (bzdOS). Source bsdOS/lifecycled; prebuilt FreeBSD
binary via bsdOS's own build/deploy pipeline; rc.d bsdos_lifecycled.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket

log = logging.getLogger("jailrun.lifecycle")

DEFAULT_SOCK = os.getenv("JAILRUN_LIFECYCLED_SOCK", "/var/run/bsdos-lifecycle.sock")
_VERBS = {"FREEZE", "THAW", "HIBERNATE", "RESTORE", "KILL", "STATUS"}
_TIMEOUT_S = 10.0


# NotAvailable: RuntimeError subtype raised when a lifecycled-only verb is
#   requested (FREEZE/THAW/HIBERNATE/RESTORE) but the daemon socket is absent
class NotAvailable(RuntimeError):
    """Raised when a lifecycled-only op is requested but the daemon is absent."""


# Lifecycled:start
#   purpose: thin synchronous client for the bsdos_lifecycled AF_UNIX stream socket
#   intent: encapsulates wire protocol (one-line request, one-line JSON response) so
#           callers use named methods and get plain dicts back; stateless except sock_path
#   sideEffects: none at construction; each method call opens a TCP-equivalent AF_UNIX
#                socket to sock_path, sends a line, receives a line, closes the socket
# Lifecycled:end
class Lifecycled:
    """Thin synchronous client for the bsdos_lifecycled Unix socket."""

    # __init__: stores sock_path; no I/O (returns True/False for available())
    def __init__(self, sock_path: str = DEFAULT_SOCK) -> None:
        self.sock_path = sock_path

    # available: returns True if sock_path exists on filesystem (no handshake, best-effort)
    def available(self) -> bool:
        """True if the daemon socket is present (best-effort, no handshake)."""
        return os.path.exists(self.sock_path)

    # _cmd:start
    #   purpose: send a single verb+jail command to bsdos_lifecycled and return parsed response
    #   input:
    #     verb: str — one of FREEZE|THAW|HIBERNATE|RESTORE|KILL|STATUS (uppercased internally)
    #     jail: str — jail name passed after the verb; empty string for STATUS
    #   output:
    #     result: dict — parsed JSON response {"ok": bool, "msg"/"err": ...} or
    #                    {"ok": True, "raw": <unparsed line>} on JSON decode failure
    #   sideEffects: connects AF_UNIX SOCK_STREAM to self.sock_path; sends encoded line;
    #                receives up to 65536 bytes per chunk until newline or EOF; closes socket
    #   rationale: loop reads until newline because daemon answers exactly one line per command;
    #              fallback to raw dict avoids hard crash on unexpected daemon output
    def _cmd(self, verb: str, jail: str = "") -> dict:
        verb = verb.upper()
        if verb not in _VERBS:
            raise ValueError(f"unknown lifecycle verb: {verb!r}")
        if not self.available():
            raise NotAvailable(f"bsdos_lifecycled socket not found: {self.sock_path}")

        line = f"{verb} {jail}".strip() + "\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_TIMEOUT_S)
            s.connect(self.sock_path)
            s.sendall(line.encode())
            chunks: list[bytes] = []
            while True:
                buf = s.recv(65536)
                if not buf:
                    break
                chunks.append(buf)
                if b"\n" in buf:  # daemon answers one line per command
                    break
        raw = b"".join(chunks).decode(errors="replace").strip()
        try:
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return {"ok": True, "raw": raw}
    # _cmd:end

    # freeze:start
    #   purpose: ask lifecycled to SIGSTOP all processes in jail (0% CPU, state in RAM)
    #   input:
    #     jail: str — jail name (jailrun uses "jailrun-<handle>")
    #   output:
    #     result: dict — lifecycled JSON response {"ok": bool, ...}
    #   sideEffects: opens AF_UNIX socket to self.sock_path; sends "FREEZE <jail>\n"; reads response
    def freeze(self, jail: str) -> dict:
        return self._cmd("FREEZE", jail)
    # freeze:end

    # thaw:start
    #   purpose: ask lifecycled to SIGCONT all frozen jail processes (instant resume)
    #   input:
    #     jail: str — jail name
    #   output:
    #     result: dict — lifecycled JSON response {"ok": bool, ...}
    #   sideEffects: opens AF_UNIX socket to self.sock_path; sends "THAW <jail>\n"; reads response
    def thaw(self, jail: str) -> dict:
        return self._cmd("THAW", jail)
    # thaw:end

    # hibernate:start
    #   purpose: ask lifecycled to take a ZFS snapshot then SIGSTOP jail procs (RAM-light suspend)
    #   input:
    #     jail: str — jail name
    #   output:
    #     result: dict — lifecycled JSON response {"ok": bool, ...}
    #   sideEffects: opens AF_UNIX socket to self.sock_path; sends "HIBERNATE <jail>\n";
    #                reads response (daemon side performs zfs snapshot + SIGSTOP)
    def hibernate(self, jail: str) -> dict:
        return self._cmd("HIBERNATE", jail)
    # hibernate:end

    # restore:start
    #   purpose: ask lifecycled to thaw a frozen jail or restore it from ZFS snapshot
    #   input:
    #     jail: str — jail name
    #   output:
    #     result: dict — lifecycled JSON response {"ok": bool, ...}
    #   sideEffects: opens AF_UNIX socket to self.sock_path; sends "RESTORE <jail>\n"; reads response
    def restore(self, jail: str) -> dict:
        return self._cmd("RESTORE", jail)
    # restore:end

    # kill:start
    #   purpose: ask lifecycled to SIGKILL all jail procs and run bsdOS cleanup
    #   input:
    #     jail: str — jail name
    #   output:
    #     result: dict — lifecycled JSON response {"ok": bool, ...}
    #   sideEffects: opens AF_UNIX socket to self.sock_path; sends "KILL <jail>\n";
    #                reads response (daemon side sends SIGKILL to all jail PIDs + bsdOS cleanup)
    #   rationale: engine always follows with `jail -r`; this is the process half of --rm
    def kill(self, jail: str) -> dict:
        return self._cmd("KILL", jail)
    # kill:end

    # status:start
    #   purpose: retrieve list of all jails tracked by lifecycled with their states and PIDs
    #   input: none
    #   output:
    #     result: dict — {"ok": true, "jails": [{app_id, jid, state, pids}, ...]}
    #   sideEffects: opens AF_UNIX socket to self.sock_path; sends "STATUS\n"; reads response
    def status(self) -> dict:
        return self._cmd("STATUS")
    # status:end


# teardown:start
#   purpose: best-effort signal jail process termination via lifecycled before engine teardown
#   input:
#     jail_name: str — jail name passed to lifecycled KILL verb
#     sock_path: str — path to bsdos_lifecycled Unix socket (default from env/DEFAULT_SOCK)
#   output:
#     result: None — always; errors are caught and logged, never re-raised
#   sideEffects: if daemon is present: runs asyncio.to_thread(lc.kill, jail_name) which opens
#                AF_UNIX socket and sends KILL command; writes to log at INFO on success,
#                WARNING on failure, DEBUG if daemon absent
#   rationale: engine always follows with `jail -r` (removes persist jail) and optionally
#              store.destroy (zfs destroy + unmount); so a missing or failing daemon is
#              non-fatal — the jail and its procs are removed by jail(8) regardless
async def teardown(jail_name: str, *, sock_path: str = DEFAULT_SOCK) -> None:
    """
    Stop a jail's processes the bzdOS way, then leave it to the engine to
    `jail -r` + ZFS-destroy.

    Step 1: ask bsdos_lifecycled to KILL (SIGKILL all PIDs + bsdOS cleanup), if the
            daemon is present — best-effort, never fatal.
    The engine ALWAYS follows with `jail -r` (removes the persist jail) and, on
    --rm, `store.destroy` (zfs destroy + unmount). So a missing daemon is fine.
    """
    lc = Lifecycled(sock_path)
    # START_CHECK_DAEMON_PRESENCE
    if not lc.available():
        log.debug("bsdos_lifecycled absent (%s) — engine's jail -r handles teardown", sock_path)
        return
    # END_CHECK_DAEMON_PRESENCE
    # START_SEND_KILL
    try:
        res = await asyncio.to_thread(lc.kill, jail_name)
        log.info("bsdos_lifecycled KILL %s -> %s", jail_name, res)
    except Exception as exc:  # noqa: BLE001
        log.warning("bsdos_lifecycled KILL %s failed (non-fatal): %s", jail_name, exc)
    # END_SEND_KILL
# teardown:end

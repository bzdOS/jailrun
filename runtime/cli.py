# START_AI_HEADER
# MODULE: runtime/cli.py
# PURPOSE: docker-run-compatible CLI argument parser and subcommand dispatcher for jailrun
# INTENT: front-door for the jailrun binary; keeps parsing logic isolated so engine.py
#         and lifecycle.py have no argparse dependency and can be imported in tests
# DEPENDENCIES: stdlib (argparse, sys, sqlite3, json); runtime.engine.run;
#               runtime.engine.exec_run/_check_jail_live (exec); runtime.engine.
#               _store_module/_load_manifest (explain IMAGE path, pull);
#               runtime.explain.render_explain; runtime.lifecycle.Lifecycled;
#               runtime.rundb.RunDB (ps, logs); runtime.doctor (doctor); runtime.gc (gc);
#               runtime.scan.scan_image/aggregate/render (scan)
# PUBLIC_API: main(argv) -> int
# END_AI_HEADER

"""
jailrun CLI — docker-run-compatible argument parser for the jailrun runtime.

Entry point: `jailrun` dispatches to subcommands:
  jailrun run [FLAGS] IMAGE [CMD [ARGS...]]
  jailrun exec [FLAGS] JAIL_NAME CMD [ARGS...]
  jailrun pull IMAGE [--authfile PATH] [--creds USER:PASS]
  jailrun ps [--all]
  jailrun logs JAIL_NAME
  jailrun stop [--rm] JAIL_NAME
  jailrun rm JAIL_NAME
  jailrun wait JAIL_NAME
  jailrun inspect JAIL_NAME [--format text|json]
  jailrun explain [--manifest FILE | IMAGE] [--format text|json]
  jailrun scan IMAGE [IMAGE...] [--format text|json]
  jailrun doctor [--format text|json]
  jailrun gc [--fix] [--format text|json]
  jailrun version

Design notes:
- Mirror docker run flag surface so existing tooling / docs translate 1-to-1.
- Keep this file pure-parsing: no I/O, no subprocess. engine.run()/exec_run()
  do the actual work — the jls(8)/rundb liveness check `exec` needs before
  dispatch also lives in engine.py (_check_jail_live), not here, for the same
  reason.
- py_compile-clean; mocks for unbuilt seams (store, probe, bakery) live in engine.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid


# ---------------------------------------------------------------------------
# Version — auto-detected from git describe, VERSION file, or default.
# ---------------------------------------------------------------------------
from runtime._version import jailrun_version, platform_info, version_banner

JAILRUN_VERSION = jailrun_version()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# _kv_pair: parses "KEY=VALUE" string into (key, value) tuple; raises ArgumentTypeError on bad input (pure, no IO)
def _kv_pair(s: str) -> tuple[str, str]:
    """Parse KEY=VALUE into (key, value). Raises ArgumentTypeError on bad input."""
    if "=" not in s:
        raise argparse.ArgumentTypeError(
            f"-e / --env expects KEY=VALUE, got: {s!r}"
        )
    k, _, v = s.partition("=")
    if not k:
        raise argparse.ArgumentTypeError(
            f"-e / --env key must not be empty, got: {s!r}"
        )
    return (k, v)


# CONTRACT: split on ':' -> validate 2-3 parts + non-empty paths -> parse optional ro/rw flag -> return (host, ctr, readonly)
def _volume_spec(s: str) -> tuple[str, str, bool]:
    """
    Parse HOST:CONTAINER[:ro] into (host_path, ctr_path, readonly).
    Raises ArgumentTypeError on bad input.
    """
    parts = s.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise argparse.ArgumentTypeError(
            f"-v / --volume expects HOST:CONTAINER[:ro], got: {s!r}"
        )
    host, ctr = parts[0], parts[1]
    if not host or not ctr:
        raise argparse.ArgumentTypeError(
            f"-v / --volume host and container paths must not be empty, got: {s!r}"
        )
    readonly = False
    if len(parts) == 3:
        flag = parts[2].lower()
        if flag not in ("ro", "rw"):
            raise argparse.ArgumentTypeError(
                f"-v / --volume mode must be 'ro' or 'rw', got: {flag!r}"
            )
        readonly = flag == "ro"
    return (host, ctr, readonly)


# _creds_pair: parses "USER:PASS" string into (user, password) tuple; raises ArgumentTypeError on bad input (pure, no IO)
def _creds_pair(s: str) -> tuple[str, str]:
    """
    Parse USER:PASS into (user, password). Raises ArgumentTypeError on bad input.

    Splits on the FIRST ':' only (matching skopeo's own --creds parsing) —
    the user portion cannot contain ':', but the password can.
    """
    if ":" not in s:
        raise argparse.ArgumentTypeError(
            f"--creds expects USER:PASS, got: {s!r}"
        )
    user, _, password = s.partition(":")
    if not user:
        raise argparse.ArgumentTypeError(
            f"--creds user must not be empty, got: {s!r}"
        )
    return (user, password)


# _publish_spec: parses "HOST_PORT:CTR_PORT[/PROTO]" into (host_port, ctr_port, proto) — pure, no IO
def _publish_spec(s: str) -> tuple[int, int, str]:
    """
    Parse HOST_PORT:CTR_PORT[/PROTO] into (host_port, ctr_port, proto).
    Raises ArgumentTypeError on bad input.
    """
    proto = "tcp"
    rest = s
    if "/" in s:
        rest, _, proto = s.rpartition("/")
        if proto not in ("tcp", "udp"):
            raise argparse.ArgumentTypeError(
                f"protocol must be 'tcp' or 'udp', got: {proto!r}"
            )
    if ":" not in rest:
        raise argparse.ArgumentTypeError(
            f"-p / --publish expects HOST_PORT:CTR_PORT[/PROTO], got: {s!r}"
        )
    host_part, _, ctr_part = rest.partition(":")
    if not host_part.isdigit() or not ctr_part.isdigit():
        raise argparse.ArgumentTypeError(
            f"-p / --publish ports must be numbers, got: {s!r}"
        )
    host_port = int(host_part)
    ctr_port = int(ctr_part)
    if not (1 <= host_port <= 65535) or not (1 <= ctr_port <= 65535):
        raise argparse.ArgumentTypeError(
            f"-p / --publish ports must be 1..65535, got: {s!r}"
        )
    return (host_port, ctr_port, proto)


# ---------------------------------------------------------------------------
# Sub-parsers
# ---------------------------------------------------------------------------

# CONTRACT: create root ArgumentParser -> add_subparsers -> register run/ps/version/freeze/thaw/hibernate/restore -> return parser
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jailrun",
        description="Native-first OCI runtime for FreeBSD jails.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="COMMAND")
    sub.required = True

    # ---- run ----------------------------------------------------------------
    run_p = sub.add_parser(
        "run",
        help="Run a command in a new jail container",
        description=(
            "Run a command in a new jail container from IMAGE.\n"
            "Mirrors `docker run` flags; native FreeBSD binaries shadow Linux\n"
            "equivalents per the substitution manifest; Linuxulator enabled only\n"
            "when needed."
        ),
    )
    run_p.add_argument(
        "--rm",
        action="store_true",
        default=False,
        help="Automatically remove the rootfs clone when the container exits",
    )
    run_p.add_argument(
        "-v", "--volume",
        dest="volumes",
        metavar="HOST:CTR[:ro]",
        action="append",
        type=_volume_spec,
        default=[],
        help=(
            "Bind-mount HOST path into the jail at CTR path (nullfs). "
            "Append ':ro' for read-only. Repeatable."
        ),
    )
    run_p.add_argument(
        "-e", "--env",
        dest="env",
        metavar="KEY=VALUE",
        action="append",
        type=_kv_pair,
        default=[],
        help="Set environment variable inside the jail. Repeatable.",
    )
    run_p.add_argument(
        "-w", "--workdir",
        dest="workdir",
        metavar="DIR",
        default=None,
        help="Working directory inside the jail.",
    )
    run_p.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Kill the jexec'd command after this many seconds (default: "
            "engine.DEFAULT_JEXEC_TIMEOUT_S, 1800s). Real builds (esp-idf/"
            "platformio toolchain fetch + compile) can need more."
        ),
    )
    run_p.add_argument(
        "--network",
        dest="network",
        default="none",
        help=(
            "'none' (default): no network inside the jail (ip4/ip6 disabled) — "
            "package/toolchain provisioning already happens on the host before the "
            "jail is created, so this is safe for the native-first path. 'inherit': "
            "opt-in, shares the host's network stack — only for commands that "
            "genuinely need registry/network access at exec time. "
            "'allow=host[:port][,...]': destination-scoped egress via pf anchor "
            "(0.4) — the jail gets a dedicated loopback alias IP and only the "
            "listed destinations are reachable."
        ),
    )
    run_p.add_argument(
        "--allow-raw-sockets",
        dest="allow_raw_sockets",
        action="store_true",
        default=False,
        help=(
            "Allow raw sockets inside the jail (e.g. for ping-like diagnostics). "
            "Default off — jails are plain (no VNET), so this is host-network-wide."
        ),
    )
    run_p.add_argument(
        "--no-rctl",
        dest="rctl_enabled",
        action="store_false",
        default=True,
        help="Disable rctl resource limits (CPU/memory/process/disk-IO). Default on.",
    )
    run_p.add_argument(
        "--allow-unrestricted-devfs",
        dest="allow_unrestricted_devfs",
        action="store_true",
        default=False,
        help=(
            "[SECURITY] Opt in to running with an unrestricted devfs ruleset "
            "when jailrun itself is nested (running inside a jail) AND that "
            "outer jail does not delegate devfs ruleset 4 to its children "
            "(missing allow.mount.devfs/enforce_statfs/devfs_ruleset=4 on the "
            "outer jail -- run `jailrun doctor` to check). When delegation "
            "IS configured, nested run-jails get ruleset 4 automatically and "
            "this flag is never needed. Without it, a nested jailrun defaults "
            "to REFUSING to run rather than silently exposing /dev/mem etc. "
            "to the sandboxed command. Verified live: /dev/mem is actually "
            "readable from inside such an unrestricted run-jail. Pass this "
            "flag ONLY for trusted/manual testing, never for untrusted-code "
            "compiles. Default: off (fail closed)."
        ),
    )
    run_p.add_argument(
        "--rctl-rule",
        dest="rctl_rules",
        metavar="ACTION:VALUE",
        action="append",
        default=None,
        help=(
            "Override the default rctl rule set. Repeatable, e.g. "
            "--rctl-rule pcpu:deny=200 --rctl-rule memoryuse:deny=4g. "
            "Replaces (does not merge with) the built-in defaults."
        ),
    )
    run_p.add_argument(
        "--cpus",
        dest="cpus",
        type=float,
        default=None,
        metavar="N",
        help=(
            "CPU core limit (e.g. --cpus=2). Maps to cputime:sigkill "
            "rctl rule (N × 3600 CPU-seconds). Default: no per-run override."
        ),
    )
    run_p.add_argument(
        "--memory",
        dest="memory",
        type=str,
        default=None,
        metavar="SIZE",
        help=(
            "Memory limit (e.g. --memory=4g, --memory=512m). Maps to "
            "memoryuse:sigkill rctl rule. Default: 8g (jailrun's built-in default)."
        ),
    )
    run_p.add_argument(
        "-it",
        action="store_true",
        dest="interactive",
        default=False,
        help=(
            "Allocate a pseudo-TTY and keep stdin open "
            "(mirrors docker -it; stub — interactive PTY not yet implemented)."
        ),
    )
    run_p.add_argument(
        "--name",
        dest="name",
        metavar="NAME",
        default=None,
        help="Assign a name to the jail (must be unique on this host).",
    )
    run_p.add_argument(
        "-d", "--detach",
        action="store_true",
        dest="detach",
        default=False,
        help=(
            "Run the jail in background (detached). "
            "Prints the jail/container name and returns immediately. "
            "Use `jailrun stop NAME` to stop it."
        ),
    )
    run_p.add_argument(
        "--read-only",
        action="store_true",
        dest="read_only",
        default=False,
        help=(
            "Mount the rootfs dataset read-only (ZFS readonly=on). "
            "Combine with --tmpfs for writable temp directories."
        ),
    )
    run_p.add_argument(
        "--tmpfs",
        dest="tmpfs",
        metavar="PATH[:SIZE]",
        action="append",
        default=None,
        help=(
            "Mount a tmpfs at PATH inside the jail. "
            "Optionally append ':SIZE' (e.g. --tmpfs /tmp:512m). "
            "Repeatable. Useful with --read-only."
        ),
    )
    run_p.add_argument(
        "-p", "--publish",
        dest="publish",
        metavar="HOST_PORT:CTR_PORT[/PROTO]",
        action="append",
        type=_publish_spec,
        default=None,
        help=(
            "Publish a host port to the jail (port forwarding). "
            "Protocol defaults to tcp. Requires --network vnet. "
            "Repeatable, e.g. -p 8080:80 -p 53:53/udp."
        ),
    )
    # IMAGE is positional; everything after it is the command.
    run_p.add_argument(
        "image",
        metavar="IMAGE",
        help="OCI image reference, e.g. alpine:3.19 or esphome/esphome:2025.5",
    )
    run_p.add_argument(
        "cmd",
        metavar="CMD",
        nargs=argparse.REMAINDER,
        help="Command (and arguments) to run inside the jail.",
    )

    # ---- exec -----------------------------------------------------------
    exec_p = sub.add_parser(
        "exec",
        help="Run an additional command inside an already-running jail",
        description=(
            "Run CMD inside a jail that a PRIOR `jailrun run` already created\n"
            "and left running — the same shape as `docker exec`. Verifies\n"
            "JAIL_NAME is live (jls -n name is authoritative; the run-state\n"
            "db is consulted only as best-effort corroboration) before\n"
            "running anything.\n"
            "\n"
            "Non-interactive only: no -it/pseudo-TTY allocation (a separate,\n"
            "larger task) — streamed stdout/stderr, the same shape `jailrun\n"
            "run` itself already provides. SIGINT/SIGTERM sent to this CLI\n"
            "process are forwarded to the exec'd command specifically (not\n"
            "the whole jail, which may have other work running in it)."
        ),
    )
    exec_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="Name of an already-running jail (e.g. jailrun-<handle>), as shown by `jailrun ps`.",
    )
    exec_p.add_argument(
        "-e", "--env",
        dest="env",
        metavar="KEY=VALUE",
        action="append",
        type=_kv_pair,
        default=[],
        help="Set environment variable for this exec'd command. Repeatable.",
    )
    exec_p.add_argument(
        "-w", "--workdir",
        dest="workdir",
        metavar="DIR",
        default=None,
        help="Working directory inside the jail for this exec'd command.",
    )
    exec_p.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Kill this exec'd command after this many seconds (default: "
            "engine.DEFAULT_JEXEC_TIMEOUT_S, 1800s)."
        ),
    )
    exec_p.add_argument(
        "-d", "--detach",
        action="store_true",
        dest="detach",
        default=False,
        help="Run the command in background (detached). Output goes to the jail's log file.",
    )
    # JAIL_NAME is positional; everything after it is the command (mirrors
    # `run`'s IMAGE + cmd REMAINDER pattern above).
    exec_p.add_argument(
        "cmd",
        metavar="CMD",
        nargs=argparse.REMAINDER,
        help="Command (and arguments) to run inside the jail.",
    )

    # ---- pull -----------------------------------------------------------
    pull_p = sub.add_parser(
        "pull",
        help="Pre-warm the local image cache (resolve + unpack), no jail run",
        description=(
            "Resolve (skopeo copy) and unpack (umoci/bsdtar extraction + zfs\n"
            "snapshot or plaindir sentinel) IMAGE into the local store cache,\n"
            "without creating a jail or running any command — the same first\n"
            "two steps `jailrun run IMAGE ...` takes before it ever touches\n"
            "jail(8). Useful to pre-warm a batch of images ahead of time, or\n"
            "to separate network-pull time from the rest of a run when timing\n"
            "something. FreeBSD-host only (needs the real store seam: skopeo,\n"
            "umoci-or-bsdtar, zfs-or-plaindir)."
        ),
    )
    pull_p.add_argument(
        "image",
        metavar="IMAGE",
        help="OCI image reference, e.g. alpine:3.19 or alpine@sha256:<digest>",
    )
    pull_p.add_argument(
        "--authfile",
        dest="authfile",
        metavar="PATH",
        default=None,
        help=(
            "Path to a docker/podman-style JSON registry credentials file "
            "(skopeo --authfile). Overridden by --creds if both are given."
        ),
    )
    pull_p.add_argument(
        "--creds",
        dest="creds",
        metavar="USER:PASS",
        type=_creds_pair,
        default=None,
        help="Registry credentials as USER:PASS (skopeo --creds). Takes precedence over --authfile.",
    )

    # ---- ps -----------------------------------------------------------------
    ps_p = sub.add_parser(
        "ps",
        help="List jailrun-managed jail runs",
        description=(
            "List jailrun-managed jail runs from the run-state db\n"
            "(runtime/rundb.py). Shows only 'running' rows by default;\n"
            "pass --all to include exited/killed history."
        ),
    )
    ps_p.add_argument(
        "-a", "--all",
        dest="all",
        action="store_true",
        default=False,
        help="Show all runs, including exited/killed (default: running only).",
    )

    # ---- logs -----------------------------------------------------------
    logs_p = sub.add_parser(
        "logs",
        help="Show a run's captured stdout/stderr (from the run-state db)",
        description=(
            "Print a completed (or in-progress) run's captured stdout/stderr,\n"
            "looked up via the run-state db (runtime/rundb.py)'s log_path\n"
            "column — the same way `docker logs` retrieves a container's\n"
            "output after the fact."
        ),
    )
    logs_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="jail name (e.g. jailrun-<handle>), as shown by `jailrun ps`.",
    )
    logs_p.add_argument(
        "-f", "--follow",
        action="store_true",
        dest="follow",
        default=False,
        help="Follow log output in real time (like `tail -f`).",
    )

    # ---- explain --------------------------------------------------------
    explain_p = sub.add_parser(
        "explain",
        help="Explain whether/how an image runs under jailrun",
        description=(
            "Answer 'will this image run under jailrun, how, and what would\n"
            "make it better' by rendering the substitution manifest: which\n"
            "binaries are native, which fall back to Linuxulator and why, and\n"
            "the concrete pkg/port fix that would flip a given binary to native.\n"
            "\n"
            "Two ways to get a manifest:\n"
            "  --manifest FILE   load a pre-produced JSON manifest from disk.\n"
            "                    Works on any host (Linux or FreeBSD).\n"
            "  IMAGE             resolve the manifest the same way `jailrun run`\n"
            "                    does (resolve -> unpack -> clone -> probe/bakery).\n"
            "                    FreeBSD-host only — needs the real store/probe/\n"
            "                    bakery seams (ZFS, jail tooling)."
        ),
    )
    explain_p.add_argument(
        "--manifest",
        dest="manifest",
        metavar="FILE",
        default=None,
        help="Load a substitution manifest JSON file from disk instead of resolving IMAGE.",
    )
    explain_p.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    explain_p.add_argument(
        "image",
        metavar="IMAGE",
        nargs="?",
        default=None,
        help=(
            "OCI image reference to resolve a manifest for. FreeBSD-host only "
            "(requires the real store/probe/bakery seams). Omit if --manifest is given."
        ),
    )

    # ---- scan -----------------------------------------------------------
    scan_p = sub.add_parser(
        "scan",
        help="Aggregate native-vs-linuxulator compat stats across multiple images",
        description=(
            "Run jailrun's own probe() against one or more images and report\n"
            "an aggregate 'how native is this image' summary — the same\n"
            "per-binary data `jailrun explain` renders for ONE image, rolled\n"
            "up across a batch: total images, overall native-binary %\n"
            "across all of them, and images ranked by native % (highest and\n"
            "lowest).\n"
            "\n"
            "FreeBSD-host only (needs the real store/probe seams, same as\n"
            "`jailrun explain IMAGE`). A bad/unreachable image reference is\n"
            "reported per-image and does not abort scanning the rest."
        ),
    )
    scan_p.add_argument(
        "images",
        metavar="IMAGE",
        nargs="+",
        help="One or more OCI image references, e.g. alpine:3.19 debian:12",
    )
    scan_p.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )

    # ---- version ------------------------------------------------------------
    sub.add_parser(
        "version",
        help="Show jailrun version information",
    )

    # ---- help ---------------------------------------------------------------
    help_p = sub.add_parser(
        "help",
        help="Show detailed help (man-page-level detail)",
        description=(
            "Display the jailrun man page (via `man` if available, "
            "or a text summary)."
        ),
    )
    help_p.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Subcommand to get help for (default: overall usage).",
    )

    # ---- doctor ------------------------------------------------------------
    doctor_p = sub.add_parser(
        "doctor",
        help="Inspect host and report jailrun readiness",
        description=(
            "Inspect the host and report jailrun readiness.\n"
            "Checks for required tools (skopeo, bsdtar), kernel modules (linux64),\n"
            "and FreeBSD-specific config (ZFS pool, racct, pkg trust keys).\n"
            "Provides exact fix text on failure."
        ),
    )
    doctor_p.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        default="text",
        help="Output format: 'text' (default, human-readable) or 'json'.",
    )

    # ---- gc -----------------------------------------------------------------
    gc_p = sub.add_parser(
        "gc",
        help="Find and clean up orphans left behind by a jailrun crash",
        description=(
            "Reconcile `jls -n`, the run-state db (runtime/rundb.py), and the\n"
            "store's runs dataset/directory tree to find crash artifacts left\n"
            "behind when jailrun's OWN process is killed mid-run (not the jailed\n"
            "workload crashing — engine.py's finally/timeout logic already\n"
            "handles that case): stale 'running' rundb rows whose jail is gone,\n"
            "live jailrun-* jails with no (or stale) rundb row, and orphaned\n"
            "ZFS clones/plaindir copies.\n"
            "\n"
            "Default: dry-run report only (exit 0 if clean, 1 if anything is\n"
            "found — usable as a health check in scripts). --fix: actually\n"
            "clean up what was found."
        ),
    )
    gc_p.add_argument(
        "--fix",
        dest="fix",
        action="store_true",
        default=False,
        help="Actually clean up detected orphans (default: dry-run report only).",
    )
    gc_p.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        default="text",
        help="Output format: 'text' (default, human-readable) or 'json'.",
    )

    # ---- jail lifecycle (native jexec kill) ----------------------------------
    for verb, helptext in (
        ("freeze", "SIGSTOP a jail's processes (0%% CPU, state stays in RAM)"),
        ("thaw", "SIGCONT a frozen jail (instant resume)"),
        ("hibernate", "ZFS-snapshot + SIGSTOP a jail (RAM-light)"),
        ("restore", "Restore a hibernated/frozen jail"),
    ):
        lp = sub.add_parser(
            verb,
            help=helptext,
            description=f"{helptext}\nNative jexec kill (see runtime/lifecycle.py).",
        )
        lp.add_argument("jail", help="jail name (e.g. jailrun-<handle>)")

    # ---- stop ----------------------------------------------------------------
    stop_p = sub.add_parser(
        "stop",
        help="Stop a running jail (kill processes, remove jail, record exit)",
        description=(
            "Stop a running jail managed by jailrun: SIGKILL all processes\n"
            "inside the jail (via jexec kill -KILL -1), remove the persist\n"
            "jail (jail -r), and update the run-state db to status='killed'.\n"
            "\n"
            "Does NOT destroy the clone/rootfs dataset by default — pass\n"
            "--rm to also reclaim that storage (or run `jailrun gc --fix`\n"
            "later to clean up orphaned datasets)."
        ),
    )
    stop_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="Name of a running jail (e.g. jailrun-<id> or the --name used at creation).",
    )
    stop_p.add_argument(
        "--rm",
        action="store_true",
        dest="rm",
        default=False,
        help=(
            "Also destroy the clone/rootfs dataset after stopping "
            "(like `jailrun run --rm` does on normal exit)."
        ),
    )

    # ---- rm ------------------------------------------------------------------
    rm_p = sub.add_parser(
        "rm",
        help="Remove a stopped jail's clone/rootfs dataset",
        description=(
            "Destroy the ZFS clone or plaindir copy backing a stopped jail.\n"
            "The jail must NOT be running (stop it first with `jailrun stop`).\n"
            "Does not modify the run-state db entry itself."
        ),
    )
    rm_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="Name of a stopped jail (e.g. jailrun-<id> or the --name used at creation).",
    )

    # ---- wait ----------------------------------------------------------------
    wait_p = sub.add_parser(
        "wait",
        help="Follow a running jail's log output (tail -f, like docker attach)",
        description=(
            "Tail the log file of a running (or recently completed) jail.\n"
            "Follows the file in real-time until the jail stops or the\n"
            "user presses Ctrl-C."
        ),
    )
    wait_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="Name of a running (or recently-completed) jail.",
    )

    # ---- inspect --------------------------------------------------------------
    inspect_p = sub.add_parser(
        "inspect",
        help="Show detailed information about a jail (JSON)",
        description=(
            "Print a JSON object with the jail's run-state, live status,\n"
            "and store backend information — the same shape as `docker inspect`.\n"
            "Combines data from the run-state db (rundb) and `jls -n`."
        ),
    )
    inspect_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="Jail name (e.g. jailrun-<id> or the --name used at creation).",
    )
    inspect_p.add_argument(
        "--format",
        dest="format",
        choices=("text", "json"),
        default="json",
        help="Output format (default: json).",
    )

    # ---- commit ---------------------------------------------------------------
    commit_p = sub.add_parser(
        "commit",
        help="Create a new image from a jail's current state (like docker commit)",
        description=(
            "Create a new OCI image from a jail's current rootfs state.\n"
            "Snapshots the run's ZFS clone and registers it as a new image\n"
            "in the store, making it available for future `jailrun run` commands.\n"
            "\n"
            "Example:\n"
            "  jailrun run -d --name build alpine:3.19 ./compile.sh\n"
            "  jailrun commit build myapp:built\n"
            "  jailrun run --rm myapp:built ./test.sh"
        ),
    )
    commit_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="Name of a jail (running or stopped) to commit.",
    )
    commit_p.add_argument(
        "image_ref",
        metavar="IMAGE_REF",
        nargs="?",
        default=None,
        help="Optional image reference tag (e.g. myapp:built). Auto-generated if omitted.",
    )

    # ---- stats ---------------------------------------------------------------
    stats_p = sub.add_parser(
        "stats",
        help="Show resource usage for a running jail",
        description=(
            "Display live resource usage (memory, CPU, processes) for a\n"
            "running jail, read from rctl(8) — the same data Docker stats shows.\n"
            "\n"
            "The jail must be running. Reads rctl current values for\n"
            "memoryuse, maxproc, and cputime."
        ),
    )
    stats_p.add_argument(
        "jail",
        metavar="JAIL_NAME",
        help="Name of a running jail.",
    )

    # ---- cp ------------------------------------------------------------------
    cp_p = sub.add_parser(
        "cp",
        help="Copy files between host and jail (like docker cp)",
        description=(
            "Copy files or directories between the host filesystem and\n"
            "a jail. JAIL_NAME:PATH prefixes a path inside the jail.\n"
            "\n"
            "Examples:\n"
            "  jailrun cp mybuild:/artifact.bin ./\n"
            "  jailrun cp ./config.yaml mybuild:/etc/config.yaml"
        ),
    )
    cp_p.add_argument(
        "source",
        metavar="SOURCE",
        help="Source path (host:/path or JAIL_NAME:path).",
    )
    cp_p.add_argument(
        "dest",
        metavar="DEST",
        help="Destination path (host:/path or JAIL_NAME:path).",
    )

    return parser


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# _cmd_run:start
#   purpose: assemble opts dict from parsed args and delegate to engine.run()
#   input:
#     args: argparse.Namespace — parsed flags from the 'run' subparser (image, cmd, rm, volumes, env, workdir, interactive)
#   output:
#     exit_code: int — 0 on success, non-zero on engine or jail failure
#   sideEffects: calls runtime.engine.run() which pulls OCI image via skopeo, clones ZFS dataset,
#                spawns jail(8) process, mounts nullfs volumes, sets jail env
# _cmd_run:end
def _rctl_rules_from_args(args: argparse.Namespace) -> list[str] | None:
    """Build the rctl_rules list from --rctl-rule, --cpus, --memory.

    Returns None when no rules are specified at all (engine uses defaults).
    Returns a list (possibly empty) of rule strings when any override is given,
    which replaces the engine defaults entirely.
    """
    rules: list[str] = list(args.rctl_rules) if args.rctl_rules else []
    if args.cpus is not None:
        cpu_seconds = int(args.cpus * 3600)
        rules.append(f"cputime:sigkill={cpu_seconds}")
    if args.memory is not None:
        rules.append(f"memoryuse:sigkill={args.memory}")
    return rules if rules else None


def _cmd_run(args: argparse.Namespace) -> int:
    """Dispatch to engine.run(); return exit code."""
    from runtime.engine import run  # noqa: PLC0415  (import inside fn for testability)

    if args.detach:
        return _cmd_run_detached(args)

    opts: dict = {
        "rm": args.rm,
        "volumes": args.volumes,        # list of (host, ctr, readonly)
        "env": dict(args.env),          # {KEY: VALUE}
        "workdir": args.workdir,
        "interactive": args.interactive,
        "network": args.network,
        "allow_raw_sockets": args.allow_raw_sockets,
        "rctl_enabled": args.rctl_enabled,
        "rctl_rules": _rctl_rules_from_args(args),
        "allow_unrestricted_devfs": args.allow_unrestricted_devfs,
        "read_only": args.read_only,
        "tmpfs": args.tmpfs,
        "publish": args.publish,
    }
    if args.timeout is not None:
        # Only set when given — engine.py's opts.get("timeout", DEFAULT_JEXEC_TIMEOUT_S)
        # would otherwise see a present-but-None key and use None as the timeout
        # (no limit at all) instead of falling back to the documented default.
        opts["timeout"] = args.timeout
    try:
        return run(image_ref=args.image, cmd=args.cmd, opts=opts)
    except RuntimeError as exc:  # noqa: BLE001 — CLI reports a clean message, never a raw traceback
        # run()'s own pre-flight checks (devfs delegation, VNET availability)
        # raise RuntimeError before any jail exists; every other failure mode
        # returns a jail's real exit code instead of raising.
        print(f"jailrun run: refused: {exc}", file=sys.stderr)
        return 1


def _cmd_run_detached(args: argparse.Namespace) -> int:
    """Run a jail in the background via runtime.runner. Print the name and exit."""
    jail_name: str = args.name or f"jailrun-{uuid.uuid4().hex[:16]}"

    # Reject duplicate names before writing anything.
    if args.name:
        from runtime.engine import _check_jail_live  # noqa: PLC0415
        live, _ = _check_jail_live(jail_name)
        if live:
            print(f"jailrun: name {jail_name!r} is already in use by a running jail", file=sys.stderr)
            return 1
        try:
            from runtime.rundb import RunDB  # noqa: PLC0415
            for r in RunDB().list_runs(status="running"):
                if r.get("jail_name") == jail_name:
                    print(
                        f"jailrun: name {jail_name!r} is already in the run-state db "
                        f"as 'running' — run `jailrun gc --fix` first",
                        file=sys.stderr,
                    )
                    return 1
        except Exception:  # noqa: BLE001
            pass

    opts: dict = {
        "rm": args.rm,
        "volumes": args.volumes,
        "env": dict(args.env),
        "workdir": args.workdir,
        "interactive": args.interactive,
        "network": args.network,
        "allow_raw_sockets": args.allow_raw_sockets,
        "rctl_enabled": args.rctl_enabled,
        "rctl_rules": _rctl_rules_from_args(args),
        "allow_unrestricted_devfs": args.allow_unrestricted_devfs,
        "jail_name": jail_name,
        "read_only": args.read_only,
        "tmpfs": args.tmpfs,
        "publish": args.publish,
    }
    if args.timeout is not None:
        opts["timeout"] = args.timeout

    job: dict = {"image_ref": args.image, "cmd": args.cmd, "opts": opts}

    fd, job_path = tempfile.mkstemp(suffix=".json", prefix=f"jailrun-{jail_name}-")
    with os.fdopen(fd, "w") as fh:
        json.dump(job, fh)

    subprocess.Popen(
        [sys.executable, "-m", "runtime.runner", job_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    print(jail_name)
    return 0


# _cmd_exec:start
#   purpose: run an ADDITIONAL command inside an ALREADY-RUNNING jail
#            (docker-exec semantics) — verify the target is live before
#            touching anything, then delegate to engine.exec_run()
#   input:
#     args: argparse.Namespace — parsed flags from the 'exec' subparser
#           (jail: str, cmd: list[str], env: list[(k,v)], workdir: str|None,
#           timeout: float|None)
#   output:
#     exit_code: int — the real exit code from the jexec'd process on
#                success; 1 if jail_name is not live (clean one-line message,
#                never a raw traceback — see runtime.engine._check_jail_live)
#                or if engine.exec_run() itself raises
#   sideEffects: imports runtime.engine lazily (matching the other _cmd_*
#                handlers) and calls its _check_jail_live() (subprocess:
#                `jls -n name`; best-effort RunDB read — see that function's
#                own contract for the exact jls-vs-rundb precedence); on a
#                live jail, calls engine.exec_run(), which runs
#                `jexec <jail_name> ...` and streams its stdout/stderr —
#                does NOT create or destroy the jail. Prints a clean one-line
#                error to stderr (never a raw traceback) on any failure.
# _cmd_exec:end
def _cmd_exec(args: argparse.Namespace) -> int:
    """Run CMD inside an already-running jail (docker-exec semantics); return exit code."""
    from runtime.engine import _check_jail_live, exec_run  # noqa: PLC0415  (import inside fn for testability)

    live, message = _check_jail_live(args.jail)
    if not live:
        print(message, file=sys.stderr)
        return 1

    if args.detach:
        return _cmd_exec_detached(args)

    opts: dict = {
        "env": dict(args.env),
        "workdir": args.workdir,
    }
    if args.timeout is not None:
        opts["timeout"] = args.timeout

    try:
        return exec_run(args.jail, args.cmd, opts)
    except Exception as exc:  # noqa: BLE001 — CLI reports a clean message, never a raw traceback
        print(f"jailrun exec: failed: {exc}", file=sys.stderr)
        return 1


def _cmd_exec_detached(args: argparse.Namespace) -> int:
    """Run CMD inside an already-running jail in the background (detached)."""
    import subprocess  # noqa: PLC0415
    from runtime.engine import _check_jail_live  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    live, message = _check_jail_live(args.jail)
    if not live:
        print(message, file=sys.stderr)
        return 1

    # Build jexec argv — mirrors engine._stream_jexec's logic
    env_prefix: list[str] = ["env"]
    for k, v in dict(args.env).items():
        env_prefix.append(f"{k}={v}")

    jexec_argv: list[str] = ["jexec", args.jail]
    if args.workdir:
        import shlex  # noqa: PLC0415
        inner = shlex.join(env_prefix + args.cmd)
        jexec_argv += ["sh", "-c", f"cd {shlex.quote(args.workdir)} && {inner}"]
    else:
        jexec_argv += env_prefix + args.cmd

    # Append output to the jail's log file (best-effort)
    log_file = None
    try:
        log_path = RunDB().get_log_path(args.jail)
        if log_path:
            log_file = open(log_path, "ab")  # noqa: SIM115 — closed by the child process
    except Exception:  # noqa: BLE001
        pass

    proc = subprocess.Popen(
        jexec_argv,
        stdin=subprocess.DEVNULL,
        stdout=log_file or subprocess.DEVNULL,
        stderr=subprocess.STDOUT if log_file else subprocess.DEVNULL,
        close_fds=True,
    )

    print(f"jailrun exec: detached PID {proc.pid}")
    return 0


# _cmd_pull:start
#   purpose: pre-warm the store's local cache for IMAGE — resolve() (skopeo
#            copy) then unpack() (umoci/bsdtar extraction + zfs snapshot or
#            plaindir sentinel) — WITHOUT creating a jail or running a
#            command, so an operator can separate network-pull time from the
#            rest of a run, or warm a batch of images ahead of time
#   input:
#     args: argparse.Namespace — parsed flags from the 'pull' subparser
#           (image: str, authfile: str | None, creds: tuple[str, str] | None)
#   output:
#     exit_code: int — 0 on success; 1 on any resolve()/unpack() failure —
#                always a clean one-line message, never a raw traceback
#   sideEffects: imports runtime.engine lazily (same pattern _cmd_explain's
#                IMAGE path already uses) and calls
#                engine._store_module.resolve(image, auth=creds,
#                authfile=authfile) then engine._store_module.unpack(image_id)
#                — the real store/skopeo/umoci-or-bsdtar/zfs-or-plaindir seam
#                (FreeBSD-host only for anything past argument parsing).
#                auth/authfile kwargs are only passed through when the
#                corresponding flag was actually given, so a plain
#                `jailrun pull IMAGE` with neither flag calls resolve(image)
#                with the exact same call shape as before registry auth
#                support existed. Deliberately stops at unpack() — does NOT
#                call clone(): clone() creates a writable per-run dataset that
#                only a matching destroy() cleans up, and `pull` has no run to
#                attach that lifetime to; calling it here would leak an
#                unreferenced run dataset every time an operator pre-warms the
#                cache. unpack()'s snapshot is the right "fully cached, ready
#                for the next `jailrun run`" stopping point, and it is
#                idempotent (re-pulling an already-cached image is a fast
#                no-op, per unpack()'s own contract). Prints a one-line
#                success/failure message to stdout/stderr via print().
# _cmd_pull:end
def _cmd_pull(args: argparse.Namespace) -> int:
    """Pre-warm the store cache for IMAGE (resolve + unpack); return exit code."""
    from runtime import engine  # noqa: PLC0415  (import inside fn for testability)

    resolve_kwargs: dict = {}
    if args.creds is not None:
        resolve_kwargs["auth"] = args.creds
    if args.authfile is not None:
        resolve_kwargs["authfile"] = args.authfile

    try:
        image_id = engine._store_module.resolve(args.image, **resolve_kwargs)
        engine._store_module.unpack(image_id)
    except Exception as exc:  # noqa: BLE001 — CLI reports a clean message, never a raw traceback
        print(f"jailrun pull: failed to pull {args.image!r}: {exc}", file=sys.stderr)
        return 1

    print(f"jailrun pull: {args.image} -> {image_id}")
    return 0


# _cmd_explain:start
#   purpose: obtain a substitution manifest (from --manifest FILE or by resolving
#            IMAGE the same way `jailrun run` does) and print runtime.explain's
#            rendering of it
#   input:
#     args: argparse.Namespace — parsed flags from the 'explain' subparser
#           (manifest: str | None, format: 'text'|'json', image: str | None)
#   output:
#     exit_code: int — 0 on success; 1 if neither --manifest nor IMAGE was given,
#                or if loading/resolving the manifest failed
#   sideEffects:
#     --manifest path: reads and json.load()s the given file from disk (Linux-safe,
#       unit-tested path — no VM, no FreeBSD tools).
#     IMAGE path (FreeBSD-host only): imports runtime.engine and calls its
#       resolve -> unpack -> clone -> _load_manifest sequence, exactly what
#       engine._run_async() does before assembling the native shadow layer —
#       i.e. it touches the real store/probe/bakery seams (ZFS clone, probe scan,
#       bakery provisioning plan). Not usable on a plain Linux dev host.
#     Always: print(runtime.explain.render_explain(manifest, fmt=args.format)) to stdout.
# _cmd_explain:end
def _cmd_explain(args: argparse.Namespace) -> int:
    """Dispatch to runtime.explain.render_explain(); return exit code."""
    import json  # noqa: PLC0415
    from runtime.explain import render_explain  # noqa: PLC0415

    if args.manifest:
        try:
            with open(args.manifest, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except OSError as exc:
            print(f"error: could not read manifest file {args.manifest!r}: {exc}", file=sys.stderr)
            return 1
        except json.JSONDecodeError as exc:
            print(f"error: {args.manifest!r} is not valid JSON: {exc}", file=sys.stderr)
            return 1
    elif args.image:
        # FreeBSD-host only: reuses engine's own resolve -> unpack -> clone ->
        # _load_manifest sequence (see engine._run_async's START_RESOLVE_AND_UNPACK
        # / START_CLONE_ROOTFS / START_LOAD_MANIFEST blocks). Imported here, not at
        # module level, so cli.py stays importable/testable on a plain Linux host
        # even though this branch itself needs the real FreeBSD store/probe/bakery
        # seams to do anything useful.
        from runtime import engine  # noqa: PLC0415

        image_id = engine._store_module.resolve(args.image)
        snapshot_id = engine._store_module.unpack(image_id)
        rootfs_path, _handle = engine._store_module.clone(snapshot_id)
        manifest = engine._load_manifest(rootfs_path, args.image)
    else:
        print(
            "error: jailrun explain needs either --manifest FILE or an IMAGE argument",
            file=sys.stderr,
        )
        return 1

    print(render_explain(manifest, fmt=args.format))
    return 0


# _cmd_scan:start
#   purpose: run scan_image() against every given image ref, catching and
#            reporting per-image failures individually, then aggregate() +
#            render() the successful summaries
#   input:
#     args: argparse.Namespace — parsed flags from the 'scan' subparser
#           (images: list[str], format: 'text'|'json')
#   output:
#     exit_code: int — 0 if at least one image scanned successfully (or the
#                list was empty of failures); 1 if EVERY given image failed
#                to scan (nothing to aggregate/report)
#   sideEffects: imports runtime.scan lazily (matching the _cmd_explain/
#                _cmd_doctor/_cmd_gc pattern); calls scan_image(image) for
#                each image, which touches the real store/probe seams
#                (FreeBSD-host only — see runtime.scan.scan_image); one
#                image's failure (bad ref, seam not built on this host, etc.)
#                is caught and printed to stderr, never aborting the rest;
#                prints the aggregate()+render() report to stdout via print()
def _cmd_scan(args: argparse.Namespace) -> int:
    """Scan one or more images, aggregate, and print the compat-matrix report."""
    from runtime.scan import scan_image, aggregate, render  # noqa: PLC0415

    summaries: list[dict] = []
    failures = 0
    for image in args.images:
        try:
            summaries.append(scan_image(image))
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — one bad image must never abort
            # the batch. SystemExit is included because probe.probe() raises it
            # directly (not a plain Exception) when a rootfs is unexpectedly
            # missing; every other realistic failure (NotImplementedError from
            # the store/probe mocks on a non-FreeBSD host, subprocess/OSError
            # from a real store, bad image ref from skopeo) is a normal Exception.
            failures += 1
            print(f"error: could not scan {image!r}: {exc}", file=sys.stderr)

    result = aggregate(summaries)
    print(render(result, fmt=args.format))

    if not summaries and failures:
        return 1
    return 0


# _render_ps_table:start
#   purpose: format run-state rows as a docker-ps-style table (pure, no I/O)
#   input:
#     rows: list[dict] — rows as returned by runtime.rundb.RunDB.list_runs()
#           (keys: jail_name, image, image_digest, dataset, status, exit_code,
#           started_at, ended_at); missing keys tolerated (rendered as "")
#   output:
#     table: str — header line "JAIL  IMAGE  STATUS  STARTED" followed by one
#            line per row, columns space-padded to the widest cell; header-only
#            (no data lines) when rows is empty
#   sideEffects: none (pure formatting; factored out so tests can exercise the
#                rendering shape without touching a real db)
# _render_ps_table:end
def _render_ps_table(rows: list[dict]) -> str:
    """Format run-state rows as a docker-ps-style table. Pure; no I/O."""
    headers = ("JAIL", "IMAGE", "STATUS", "STARTED")
    table_rows = [
        (
            str(r.get("jail_name", "")),
            str(r.get("image", "")),
            str(r.get("status", "")),
            str(r.get("started_at", "")),
        )
        for r in rows
    ]

    widths = [len(h) for h in headers]
    for row in table_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(cells: tuple) -> str:
        return "   ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [_fmt(headers)]
    lines.extend(_fmt(row) for row in table_rows)
    return "\n".join(lines)


# _cmd_ps:start
#   purpose: list jailrun-managed runs from the run-state db and print a
#            docker-ps-style table
#   input:
#     args: argparse.Namespace — parsed flags from the 'ps' subparser (all: bool)
#   output:
#     exit_code: int — always 0 (an absent/unreadable db is treated as "no runs
#                yet", not a failure — see sideEffects)
#   sideEffects: imports runtime.rundb.RunDB (lazy, matching the other _cmd_*
#                handlers) and calls .list_runs(status=None if args.all else
#                'running'), which may open/create the sqlite db at JAILRUN_DB
#                (default /var/db/jailrun/runs.db); if that raises
#                (OSError/sqlite3.Error — e.g. db path absent/unwritable on a
#                plain dev host) the error is swallowed here and treated as an
#                empty run list so `jailrun ps` still prints a clean header and
#                exits 0. Writes the rendered table to stdout via print().
# _cmd_ps:end
def _cmd_ps(args: argparse.Namespace) -> int:
    """List jailrun-managed runs from the run-state db; print a ps-style table."""
    import sqlite3  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    try:
        db = RunDB()
        rows = db.list_runs(status=None if args.all else "running")
    except (OSError, sqlite3.Error):
        # No db yet (fresh host, /var/db not writable, etc.) — that just means
        # nothing has run through jailrun here yet, not a CLI failure.
        rows = []

    print(_render_ps_table(rows))
    return 0


# _cmd_logs:start
#   purpose: print a completed (or in-progress) run's captured stdout/stderr,
#            looked up via the run-state db's log_path column, the same way
#            `docker logs` retrieves a container's output after the fact
#   input:
#     args: argparse.Namespace — parsed flags from the 'logs' subparser (jail: str)
#   output:
#     exit_code: int — 0 if a log_path was found and its file read
#                successfully; 1 for every ordinary failure case (unknown
#                jail_name, jail_name with no recorded log_path, the recorded
#                file missing/unreadable on disk, or an unusable run-state db)
#                — always a clean one-line message, never a raw traceback
#   sideEffects: imports runtime.rundb.RunDB (lazy, matching the other _cmd_*
#                handlers) and calls .get_log_path(args.jail), which may
#                open/create the sqlite db at JAILRUN_DB (default
#                /var/db/jailrun/runs.db); reads the log file at the returned
#                path (if any) and writes its contents to stdout via print()
# _cmd_logs:end
def _cmd_logs(args: argparse.Namespace) -> int:
    """Print a run's captured output via RunDB.get_log_path(); return exit code."""
    import sqlite3  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    try:
        log_path = RunDB().get_log_path(args.jail)
    except (OSError, sqlite3.Error) as exc:
        print(f"jailrun logs: could not read run-state db: {exc}", file=sys.stderr)
        return 1

    if not log_path:
        print(f"jailrun logs: no log found for {args.jail!r}", file=sys.stderr)
        return 1

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        print(f"jailrun logs: could not read log file {log_path!r}: {exc}", file=sys.stderr)
        return 1

    print(content, end="")
    if args.follow:
        sys.stdout.flush()
        return _cmd_wait(args)
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    """Stop a running jail: SIGKILL, jail -r, record exit. Optionally destroy clone."""
    from runtime.engine import _check_jail_live  # noqa: PLC0415
    from runtime.lifecycle import Lifecycled  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    live, message = _check_jail_live(args.jail)
    if not live:
        print(message, file=sys.stderr)
        return 1

    Lifecycled().kill(args.jail)

    try:
        subprocess.run(
            ["jail", "-r", args.jail],
            capture_output=True, timeout=30, check=False,
        )
    except OSError as exc:
        print(f"jailrun stop: jail -r failed: {exc}", file=sys.stderr)
        return 1

    RunDB().record_exit(args.jail, "killed", None)

    if args.rm:
        try:
            from runtime.gc import _destroy_dataset  # noqa: PLC0415
            rows = RunDB().list_runs(status=None)
            dataset: str | None = None
            for r in rows:
                if r.get("jail_name") == args.jail:
                    dataset = r.get("dataset")
                    break
            if dataset:
                ok, detail = _destroy_dataset(dataset)
                if not ok:
                    print(f"jailrun stop: dataset destroy failed: {detail}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"jailrun stop: dataset cleanup failed: {exc}", file=sys.stderr)

    print(f"jailrun: stopped {args.jail}")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    """Destroy a stopped jail's clone/rootfs dataset. Jail must not be running."""
    from runtime.engine import _check_jail_live  # noqa: PLC0415
    from runtime.gc import _destroy_dataset  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    live, message = _check_jail_live(args.jail)
    if live:
        print(
            f"jailrun rm: {args.jail!r} is still running — stop it first "
            f"(`jailrun stop {args.jail}`)",
            file=sys.stderr,
        )
        return 1

    try:
        rows = RunDB().list_runs(status=None)
    except Exception as exc:  # noqa: BLE001
        print(f"jailrun rm: could not read run-state db: {exc}", file=sys.stderr)
        return 1

    dataset: str | None = None
    for r in rows:
        if r.get("jail_name") == args.jail:
            dataset = r.get("dataset")
            break

    if not dataset:
        print(f"jailrun rm: no dataset found for {args.jail!r}", file=sys.stderr)
        return 1

    ok, detail = _destroy_dataset(dataset)
    if not ok:
        print(
            f"jailrun rm: could not destroy dataset {dataset!r}: {detail}\n"
            f"Try `jailrun gc --fix` for a more thorough cleanup.",
            file=sys.stderr,
        )
        return 1

    print(f"jailrun: removed {args.jail} ({dataset})")
    return 0


def _cmd_wait(args: argparse.Namespace) -> int:
    """Tail -f of a jail's log file. Follows until the jail stops or Ctrl-C."""
    import time  # noqa: PLC0415
    from runtime.engine import _check_jail_live  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    log_path = RunDB().get_log_path(args.jail)
    if not log_path:
        print(f"jailrun wait: no log file found for {args.jail!r}", file=sys.stderr)
        return 1

    try:
        with open(log_path, "r") as fh:
            while True:
                line = fh.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.2)
                    live, _ = _check_jail_live(args.jail)
                    if not live:
                        remaining = fh.read()
                        if remaining:
                            print(remaining, end="", flush=True)
                        break
    except KeyboardInterrupt:
        pass

    return 0


def _cmd_commit(args: argparse.Namespace) -> int:
    """Create a new image from a jail's current state (docker commit)."""
    import sqlite3  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    # Look up the jail's dataset from rundb
    try:
        rows = RunDB().list_runs(status=None)
    except (OSError, sqlite3.Error) as exc:
        print(f"jailrun commit: could not read run-state db: {exc}", file=sys.stderr)
        return 1

    dataset: str | None = None
    for r in rows:
        if r.get("jail_name") == args.jail:
            dataset = r.get("dataset")
            break

    if not dataset:
        print(f"jailrun commit: no dataset found for {args.jail!r}", file=sys.stderr)
        return 1

    try:
        from store.store import Store  # noqa: PLC0415
        store = Store()
        image_id = store.commit_image(dataset, image_ref=args.image_ref)
    except NotImplementedError as exc:
        print(f"jailrun commit: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"jailrun commit: failed: {exc}", file=sys.stderr)
        return 1

    if args.image_ref:
        print(f"{args.image_ref} ({image_id})")
    else:
        print(image_id)
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    """Show resource usage for a running jail via rctl."""
    import subprocess  # noqa: PLC0415
    from runtime.engine import _check_jail_live  # noqa: PLC0415

    live, message = _check_jail_live(args.jail)
    if not live:
        print(message, file=sys.stderr)
        return 1

    try:
        proc = subprocess.run(
            ["rctl", "-u", f"jail:{args.jail}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"jailrun stats: rctl failed: {exc}", file=sys.stderr)
        return 1

    # Parse rctl -u output: lines like "memoryuse=970752"
    stats: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "=" in line:
            key, _, val = line.partition("=")
            stats[key] = val

    mem_bytes = int(stats.get("memoryuse", "0"))
    mem_mib = mem_bytes / (1024 * 1024)
    pids = stats.get("maxproc", "0")
    cpu_sec = int(stats.get("cputime", "0"))

    print(f"JAIL       MEMORY    PIDS  CPUTIME(s)")
    print(f"{args.jail:<10} {mem_mib:>6.0f}MiB {pids:>5} {cpu_sec:>10}")
    return 0


def _cmd_cp(args: argparse.Namespace) -> int:
    """Copy files between host and jail (docker cp semantics)."""
    import subprocess  # noqa: PLC0415
    import shlex  # noqa: PLC0415

    def _parse_spec(spec: str) -> tuple[str | None, str]:
        """Parse 'JAIL_NAME:path' or '/absolute/path'. Returns (jail_name, path)."""
        if ":" in spec:
            jail, _, path = spec.partition(":")
            if jail and path:
                return jail, path
        return None, spec

    src_jail, src_path = _parse_spec(args.source)
    dst_jail, dst_path = _parse_spec(args.dest)

    # Validate: exactly one side must be a jail (the other is the host)
    if src_jail and dst_jail:
        print("jailrun cp: cannot copy between two jails", file=sys.stderr)
        return 1
    if not src_jail and not dst_jail:
        print("jailrun cp: one path must include JAIL_NAME:", file=sys.stderr)
        return 1

    # Copy FROM jail TO host
    if src_jail:
        from runtime.engine import _check_jail_live  # noqa: PLC0415
        live, message = _check_jail_live(src_jail)
        if not live:
            print(message, file=sys.stderr)
            return 1
        try:
            import os as _os  # noqa: PLC0415
            dst_parent = _os.path.dirname(dst_path) or "."
            if dst_parent != ".":
                _os.makedirs(dst_parent, exist_ok=True)
            rel_src = src_path.lstrip("/")
            src_base = _os.path.basename(rel_src)
            dst_base = _os.path.basename(dst_path)
            jexec_proc = subprocess.Popen(
                ["jexec", src_jail, "tar", "-C", "/", "-cf", "-", rel_src],
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                ["tar", "xf", "-", "--strip-components", "1", "-C", dst_parent],
                stdin=jexec_proc.stdout, check=True, timeout=60,
            )
            jexec_proc.wait()
            # If destination filename differs from source (after stripping
            # one path component), rename.
            src_base = _os.path.basename(rel_src)
            dst_base = _os.path.basename(dst_path)
            if src_base != dst_base:
                extracted = _os.path.join(dst_parent, src_base)
                if _os.path.exists(extracted):
                    _os.rename(extracted, dst_path)
        except Exception as exc:  # noqa: BLE001
            print(f"jailrun cp: failed to copy from jail: {exc}", file=sys.stderr)
            return 1

    # Copy FROM host TO jail
    if dst_jail:
        import shlex  # noqa: PLC0415
        from runtime.engine import _check_jail_live  # noqa: PLC0415
        live, message = _check_jail_live(dst_jail)
        if not live:
            print(message, file=sys.stderr)
            return 1
        try:
            import os as _os  # noqa: PLC0415
            src_dir = _os.path.dirname(src_path) or "."
            src_base = _os.path.basename(src_path)
            dst_parent = _os.path.dirname(dst_path) or "."
            if dst_parent != ".":
                subprocess.run(
                    ["jexec", dst_jail, "mkdir", "-p", dst_parent],
                    capture_output=True, timeout=10, check=False,
                )
            # Pipe tar of the source into the jail, extract, then rename
            # if the destination basename differs (mv is needed because
            # BusyBox tar inside Alpine doesn't support -s / --transform).
            tar_proc = subprocess.Popen(
                ["tar", "-C", src_dir, "-cf", "-", src_base],
                stdout=subprocess.PIPE,
            )
            subprocess.run(
                ["jexec", dst_jail, "tar", "xf", "-", "-C", dst_parent],
                stdin=tar_proc.stdout, check=True, timeout=60,
            )
            tar_proc.wait()
            dst_base = _os.path.basename(dst_path)
            if src_base != dst_base:
                extracted = _os.path.join(dst_parent, src_base)
                subprocess.run(
                    ["jexec", dst_jail, "mv", extracted, dst_path],
                    capture_output=True, timeout=10, check=False,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"jailrun cp: failed to copy to jail: {exc}", file=sys.stderr)
            return 1

    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    """Print detailed JSON info about a jail (rundb + jls)."""
    import json  # noqa: PLC0415
    from runtime.engine import _check_jail_live, _list_live_jail_names  # noqa: PLC0415
    from runtime.rundb import RunDB  # noqa: PLC0415

    info: dict = {"jail_name": args.jail}

    # Live status from jls
    jail_names = _list_live_jail_names()
    info["live"] = jail_names is not None and args.jail in jail_names

    # Run-state from rundb
    try:
        rows = RunDB().list_runs(status=None)
        for r in rows:
            if r.get("jail_name") == args.jail:
                info["rundb"] = {
                    "image": r.get("image"),
                    "image_digest": r.get("image_digest"),
                    "dataset": r.get("dataset"),
                    "status": r.get("status"),
                    "exit_code": r.get("exit_code"),
                    "started_at": r.get("started_at"),
                    "ended_at": r.get("ended_at"),
                    "log_path": r.get("log_path"),
                }
                break
    except Exception:  # noqa: BLE001
        info["rundb"] = None

    output = json.dumps(info, indent=2, default=str)
    print(output)
    return 0


# CONTRACT: format JAILRUN_VERSION + runtime/host strings -> print 3 lines to stdout -> return 0
def _cmd_version(_args: argparse.Namespace) -> int:
    print(version_banner())
    return 0


# _cmd_help:start
#   purpose: display man-page-level help; try man(1) first, fall back to argparse --help
#   input:
#     args: argparse.Namespace — parsed args; args.topic may name a subcommand
#   output:
#     exit_code: int — 0 on success, 1 on error
#   sideEffects: may spawn man(1) subprocess; falls back to argparse help
# _cmd_help:end
def _cmd_help(args: argparse.Namespace) -> int:
    if args.topic:
        sub_parser = _build_parser()._subparsers._group_actions[0].choices.get(args.topic)
        if sub_parser:
            sub_parser.print_help()
            return 0
        print(f"jailrun: no such subcommand {args.topic!r}", file=sys.stderr)
        return 1
    try:
        import subprocess, os
        man_path = os.path.join(os.path.dirname(__file__), "..", "man", "man1", "jailrun.1")
        r = subprocess.run(["man", man_path], capture_output=False, timeout=10)
        return r.returncode
    except Exception:
        pass
    # Fall back to built-in help
    _build_parser().print_help()
    return 0
# _cmd_help:end


# _cmd_doctor:start
#   purpose: run host readiness checks and print results to stdout
#   input:
#     args: argparse.Namespace — parsed flags from the 'doctor' subparser (format: 'text' or 'json')
#   output:
#     exit_code: int — 0 if all checks passed/skipped; 1 if any check failed
#   sideEffects: calls runtime.doctor.run_checks() and runtime.doctor.render() to inspect host state;
#                prints formatted results to stdout via print()
# _cmd_doctor:end
def _cmd_doctor(args: argparse.Namespace) -> int:
    """Inspect host and report jailrun readiness; return exit code."""
    from runtime.doctor import run_checks, render, exit_code_for_results  # noqa: PLC0415

    results = run_checks()
    output = render(results, fmt=args.format)
    print(output)
    return exit_code_for_results(results)


# _cmd_gc:start
#   purpose: run the gc reconciliation cycle and print the report
#   input:
#     args: argparse.Namespace — parsed flags from the 'gc' subparser
#           (fix: bool, format: 'text'|'json')
#   output:
#     exit_code: int — see runtime.gc.exit_code_for() (0 = clean/all-fixed,
#                1 = orphans found / a fix failed)
#   sideEffects: calls runtime.gc.run_gc() (jls/rundb/zfs I/O, and --fix's
#                cleanup actions when args.fix is set) and runtime.gc.render();
#                prints the formatted report to stdout via print()
# _cmd_gc:end
def _cmd_gc(args: argparse.Namespace) -> int:
    """Run the gc reconciliation cycle; print the report; return exit code."""
    from runtime.gc import run_gc, render, exit_code_for  # noqa: PLC0415

    orphans, fixes, notes = run_gc(fix=args.fix)
    output = render(orphans, fixes, notes, fmt=args.format)
    print(output)
    return exit_code_for(orphans, fixes)


# _cmd_lifecycle:start
#   purpose: dispatch freeze/thaw/hibernate/restore verb to Lifecycled daemon and print the result
#   input:
#     verb: str — one of 'freeze', 'thaw', 'hibernate', 'restore'
#     args: argparse.Namespace — must contain args.jail (jail name string)
#   output:
#     exit_code: int — 0 if Lifecycled returns ok or non-dict result; 1 on NotAvailable or res["ok"]==False
#   sideEffects: calls runtime.lifecycle.Lifecycled().<verb>(args.jail) which runs
#                jexec kill on FreeBSD; prints JSON result or error to stdout/stderr
# _cmd_lifecycle:end
def _cmd_lifecycle(verb: str, args: argparse.Namespace) -> int:
    """freeze/thaw/hibernate/restore — native jexec kill."""
    import json  # noqa: PLC0415
    from runtime.lifecycle import Lifecycled, NotAvailable  # noqa: PLC0415

    try:
        res = getattr(Lifecycled(), verb)(args.jail)
        print(json.dumps(res) if isinstance(res, dict) else str(res))
        return 0 if (not isinstance(res, dict) or res.get("ok", True)) else 1
    except NotAvailable as exc:
        print(f"unavailable: {exc}")
        return 1


# main:start
#   purpose: parse argv and dispatch to the correct subcommand handler
#   input:
#     argv: list[str] | None — argument vector; None means sys.argv[1:] (argparse default)
#   output:
#     exit_code: int — 0 on success; 1 on unknown subcommand or handler failure
#   sideEffects: calls _build_parser() to construct the argument parser; delegates to
#                _cmd_run / _cmd_pull / _cmd_ps / _cmd_logs / _cmd_explain / _cmd_scan /
#                _cmd_doctor / _cmd_gc / _cmd_version / _cmd_lifecycle which each have
#                their own side effects; argparse may write to stderr and call sys.exit
#                on bad args
# main:end
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "run":
        return _cmd_run(args)
    if args.subcommand == "exec":
        return _cmd_exec(args)
    if args.subcommand == "pull":
        return _cmd_pull(args)
    if args.subcommand == "ps":
        return _cmd_ps(args)
    if args.subcommand == "logs":
        return _cmd_logs(args)
    if args.subcommand == "stop":
        return _cmd_stop(args)
    if args.subcommand == "rm":
        return _cmd_rm(args)
    if args.subcommand == "wait":
        return _cmd_wait(args)
    if args.subcommand == "inspect":
        return _cmd_inspect(args)
    if args.subcommand == "commit":
        return _cmd_commit(args)
    if args.subcommand == "stats":
        return _cmd_stats(args)
    if args.subcommand == "cp":
        return _cmd_cp(args)
    if args.subcommand == "explain":
        return _cmd_explain(args)
    if args.subcommand == "scan":
        return _cmd_scan(args)
    if args.subcommand == "version":
        return _cmd_version(args)
    if args.subcommand == "help":
        return _cmd_help(args)
    if args.subcommand == "doctor":
        return _cmd_doctor(args)
    if args.subcommand == "gc":
        return _cmd_gc(args)
    if args.subcommand in ("freeze", "thaw", "hibernate", "restore"):
        return _cmd_lifecycle(args.subcommand, args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

# START_AI_HEADER
# MODULE: runtime/cli.py
# PURPOSE: docker-run-compatible CLI argument parser and subcommand dispatcher for jailrun
# INTENT: front-door for the jailrun binary; keeps parsing logic isolated so engine.py
#         and lifecycle.py have no argparse dependency and can be imported in tests
# DEPENDENCIES: stdlib (argparse, sys, subprocess, json); runtime.engine.run;
#               runtime.engine._store_module/_load_manifest (explain IMAGE path);
#               runtime.explain.render_explain; runtime.lifecycle.Lifecycled;
#               no external tools invoked here
# PUBLIC_API: main(argv) -> int
# END_AI_HEADER

"""
jailrun CLI — docker-run-compatible argument parser for the jailrun runtime.

Entry point: `jailrun` dispatches to subcommands:
  jailrun run [FLAGS] IMAGE [CMD [ARGS...]]
  jailrun ps
  jailrun explain [--manifest FILE | IMAGE] [--format text|json]
  jailrun version

Design notes:
- Mirror docker run flag surface so existing tooling / docs translate 1-to-1.
- Keep this file pure-parsing: no I/O, no subprocess. engine.run() does work.
- py_compile-clean; mocks for unbuilt seams (store, probe, bakery) live in engine.py.
"""

from __future__ import annotations

import argparse
import sys


# ---------------------------------------------------------------------------
# Version stub — replaced once we have a real release mechanism.
# ---------------------------------------------------------------------------
JAILRUN_VERSION = "0.1.0"


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
        choices=("none", "inherit"),
        default="none",
        help=(
            "'none' (default): no network inside the jail (ip4/ip6 disabled) — "
            "package/toolchain provisioning already happens on the host before the "
            "jail is created, so this is safe for the native-first path. 'inherit': "
            "opt-in, shares the host's network stack — only for commands that "
            "genuinely need registry/network access at exec time."
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
        "-it",
        action="store_true",
        dest="interactive",
        default=False,
        help=(
            "Allocate a pseudo-TTY and keep stdin open "
            "(mirrors docker -it; stub — interactive PTY not yet implemented)."
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

    # ---- ps -----------------------------------------------------------------
    sub.add_parser(
        "ps",
        help="List running jailrun containers (stub)",
        description=(
            "List running jailrun containers.\n"
            "STUB: queries `jls -n` and filters by jailrun-managed names."
        ),
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

    # ---- version ------------------------------------------------------------
    sub.add_parser(
        "version",
        help="Show jailrun version information",
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

    # ---- jail lifecycle (delegated to bsdos_lifecycled) ---------------------
    for verb, helptext in (
        ("freeze", "SIGSTOP a jail's processes (0%% CPU, state stays in RAM)"),
        ("thaw", "SIGCONT a frozen jail (instant resume)"),
        ("hibernate", "ZFS-snapshot + SIGSTOP a jail (RAM-light)"),
        ("restore", "Restore a hibernated/frozen jail"),
    ):
        lp = sub.add_parser(
            verb,
            help=helptext,
            description=f"{helptext}\nDelegated to bsdos_lifecycled (see runtime/lifecycle.py).",
        )
        lp.add_argument("jail", help="jail name (e.g. jailrun-<handle>)")

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
def _cmd_run(args: argparse.Namespace) -> int:
    """Dispatch to engine.run(); return exit code."""
    from runtime.engine import run  # noqa: PLC0415  (import inside fn for testability)

    opts: dict = {
        "rm": args.rm,
        "volumes": args.volumes,        # list of (host, ctr, readonly)
        "env": dict(args.env),          # {KEY: VALUE}
        "workdir": args.workdir,
        "interactive": args.interactive,
        "network": args.network,
        "allow_raw_sockets": args.allow_raw_sockets,
        "rctl_enabled": args.rctl_enabled,
        "rctl_rules": args.rctl_rules,
    }
    if args.timeout is not None:
        # Only set when given — engine.py's opts.get("timeout", DEFAULT_JEXEC_TIMEOUT_S)
        # would otherwise see a present-but-None key and use None as the timeout
        # (no limit at all) instead of falling back to the documented default.
        opts["timeout"] = args.timeout
    return run(image_ref=args.image, cmd=args.cmd, opts=opts)


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


# _cmd_ps:start
#   purpose: print a stub container listing header and placeholder row to stdout
#   input:
#     _args: argparse.Namespace — unused (no flags for ps yet)
#   output:
#     exit_code: int — always 0
#   sideEffects: writes two lines to stdout via print(); stub does not invoke jls(8)
#   rationale: real impl would run 'jls -n name path' and filter by jailrun-managed names;
#              stub exists so 'jailrun ps' parses and exits cleanly on a Linux dev host
# _cmd_ps:end
def _cmd_ps(_args: argparse.Namespace) -> int:
    """STUB: list running jailrun-managed jails."""
    import subprocess  # noqa: PLC0415
    print("CONTAINER ID   IMAGE          STATUS   COMMAND")
    # Real impl: `jls -n name path | grep jailrun-` and format.
    print("(stub — run 'jls' on freebsd-host for live data)")  # UNVERIFIED
    return 0


# CONTRACT: format JAILRUN_VERSION + runtime/host strings -> print 3 lines to stdout -> return 0
def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"jailrun version {JAILRUN_VERSION}")
    print("runtime: FreeBSD jails + ZFS (freebsd-host only)")
    print("host build: linux-host/Linux (design + scaffold)")
    return 0


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


# _cmd_lifecycle:start
#   purpose: dispatch freeze/thaw/hibernate/restore verb to Lifecycled daemon and print the result
#   input:
#     verb: str — one of 'freeze', 'thaw', 'hibernate', 'restore'
#     args: argparse.Namespace — must contain args.jail (jail name string)
#   output:
#     exit_code: int — 0 if Lifecycled returns ok or non-dict result; 1 on NotAvailable or res["ok"]==False
#   sideEffects: calls runtime.lifecycle.Lifecycled().<verb>(args.jail) which sends IPC to
#                bsdos_lifecycled; prints JSON result or error message to stdout/stderr via print()
# _cmd_lifecycle:end
def _cmd_lifecycle(verb: str, args: argparse.Namespace) -> int:
    """freeze/thaw/hibernate/restore — delegate to bsdos_lifecycled."""
    import json  # noqa: PLC0415
    from runtime.lifecycle import Lifecycled, NotAvailable  # noqa: PLC0415

    try:
        res = getattr(Lifecycled(), verb)(args.jail)
        print(json.dumps(res) if isinstance(res, dict) else str(res))
        return 0 if (not isinstance(res, dict) or res.get("ok", True)) else 1
    except NotAvailable as exc:
        print(f"unavailable: {exc}\n(this op needs bsdos_lifecycled running on a FreeBSD host)")
        return 1


# main:start
#   purpose: parse argv and dispatch to the correct subcommand handler
#   input:
#     argv: list[str] | None — argument vector; None means sys.argv[1:] (argparse default)
#   output:
#     exit_code: int — 0 on success; 1 on unknown subcommand or handler failure
#   sideEffects: calls _build_parser() to construct the argument parser; delegates to
#                _cmd_run / _cmd_ps / _cmd_version / _cmd_lifecycle which each have their
#                own side effects; argparse may write to stderr and call sys.exit on bad args
# main:end
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "run":
        return _cmd_run(args)
    if args.subcommand == "ps":
        return _cmd_ps(args)
    if args.subcommand == "explain":
        return _cmd_explain(args)
    if args.subcommand == "version":
        return _cmd_version(args)
    if args.subcommand == "doctor":
        return _cmd_doctor(args)
    if args.subcommand in ("freeze", "thaw", "hibernate", "restore"):
        return _cmd_lifecycle(args.subcommand, args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

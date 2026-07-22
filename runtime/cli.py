# START_AI_HEADER
# MODULE: runtime/cli.py
# PURPOSE: docker-run-compatible CLI argument parser and subcommand dispatcher for jailrun
# INTENT: front-door for the jailrun binary; keeps parsing logic isolated so engine.py
#         and lifecycle.py have no argparse dependency and can be imported in tests
# DEPENDENCIES: stdlib (argparse, sys, sqlite3, json); runtime.engine.run;
#               runtime.engine._store_module/_load_manifest (explain IMAGE path, pull);
#               runtime.explain.render_explain; runtime.lifecycle.Lifecycled;
#               runtime.rundb.RunDB (ps, logs); runtime.doctor (doctor); runtime.gc (gc);
#               runtime.scan.scan_image/aggregate/render (scan)
# PUBLIC_API: main(argv) -> int
# END_AI_HEADER

"""
jailrun CLI — docker-run-compatible argument parser for the jailrun runtime.

Entry point: `jailrun` dispatches to subcommands:
  jailrun run [FLAGS] IMAGE [CMD [ARGS...]]
  jailrun pull IMAGE [--authfile PATH] [--creds USER:PASS]
  jailrun ps [--all]
  jailrun logs JAIL_NAME
  jailrun explain [--manifest FILE | IMAGE] [--format text|json]
  jailrun scan IMAGE [IMAGE...] [--format text|json]
  jailrun doctor [--format text|json]
  jailrun gc [--fix] [--format text|json]
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
        "--allow-unrestricted-devfs",
        dest="allow_unrestricted_devfs",
        action="store_true",
        default=False,
        help=(
            "[SECURITY] Opt in to running with an unrestricted devfs ruleset "
            "when jailrun itself is nested (running inside a jail). Applying "
            "devfs ruleset 4 is a host-only privilege a jail cannot delegate "
            "to its own run-jails, so a nested jailrun defaults to "
            "REFUSING to run rather than silently exposing /dev/mem etc. to "
            "the sandboxed command. Verified live: /dev/mem is actually "
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
        "allow_unrestricted_devfs": args.allow_unrestricted_devfs,
    }
    if args.timeout is not None:
        # Only set when given — engine.py's opts.get("timeout", DEFAULT_JEXEC_TIMEOUT_S)
        # would otherwise see a present-but-None key and use None as the timeout
        # (no limit at all) instead of falling back to the documented default.
        opts["timeout"] = args.timeout
    return run(image_ref=args.image, cmd=args.cmd, opts=opts)


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
    if args.subcommand == "pull":
        return _cmd_pull(args)
    if args.subcommand == "ps":
        return _cmd_ps(args)
    if args.subcommand == "logs":
        return _cmd_logs(args)
    if args.subcommand == "explain":
        return _cmd_explain(args)
    if args.subcommand == "scan":
        return _cmd_scan(args)
    if args.subcommand == "version":
        return _cmd_version(args)
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

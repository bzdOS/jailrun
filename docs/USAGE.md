# jailrun — Usage

This covers the commands jailrun currently ships: `run`, `exec`, `ps`, `logs`,
`stop`, `rm`, `wait`, `explain`, `doctor`, `version`, and `gc`. The jail
lifecycle verbs `freeze`/`thaw` use native `jexec kill` — see
`runtime/lifecycle.py`.

Every example below is real output captured by actually running the command —
see the "how to reproduce" note under each one.

## What needs FreeBSD, and what doesn't

| Command | Needs a FreeBSD 15+ host? | Why |
|---|---|---|---|
| `jailrun version` | No | Prints static strings. |
| `jailrun doctor` | No | The whole point is to run *anywhere* and tell you what's missing; FreeBSD-only checks report `fail`/`skip` on other OSes instead of crashing. |
| `jailrun explain --manifest FILE` | No | Pure read + render over a JSON file already on disk — no subprocess, no jail/ZFS calls. |
| `jailrun explain IMAGE` | Yes | Resolves the manifest by running the same resolve → unpack → clone pipeline as `run` (skopeo, ZFS clone, probe). |
| `jailrun run` (foreground) | Yes | Pulls the image (skopeo), unpacks/clones it on ZFS, and spawns a real `jail(8)` + `jexec`. None of that exists outside FreeBSD. |
| `jailrun run -d` (detached) | Yes | Same as foreground, but the jexec runs in a background worker process (see `runtime/runner.py`). |
| `jailrun exec` | Yes | `jexec(8)` into an already-running jail — requires a live FreeBSD jail. |
| `jailrun ps` | Yes (reads) | Reads the run-state SQLite database (`/var/db/jailrun/runs.db`). The db path is readable on any host, but on Linux the table is simply empty. |
| `jailrun logs` | Yes (reads) | Same db dependency as `ps`; reads a log file path from rundb and dumps its contents. |
| `jailrun stop` | Yes | Runs `jexec kill -KILL -1` + `jail -r` + updates the rundb. |
| `jailrun rm` | Yes | Destroys a ZFS clone / plaindir copy — requires the store backend. |
| `jailrun wait` | No | Pure file read + polling loop; follows a log file and checks jls liveness. Harmless on Linux (reads the log file from rundb, jls returns False). |

You can clone the repo and edit/test on Linux or macOS — `py_compile` and the unit
tests don't touch FreeBSD-only code paths. See
[docs/DEV_ENVIRONMENT.md](DEV_ENVIRONMENT.md) for setting up a real FreeBSD host
to actually run images.

---

## `jailrun run`

```
jailrun run [FLAGS] IMAGE [CMD [ARGS...]]
```

Runs `CMD` inside a new jail cloned from `IMAGE`. Mirrors `docker run`'s flag
surface; native FreeBSD binaries shadow the image's Linux ones per the
substitution manifest, and the Linuxulator is only enabled when the manifest
says a binary needs it.

### Flags

| Flag | Argument | Default | Description |
|---|---|---|---|---|
| `--rm` | — | off | Automatically remove the rootfs clone when the container exits |
| `-d`, `--detach` | — | off | Run the jail in background (detached). Prints the jail name and returns immediately. Use `jailrun ps`, `jailrun logs`, `jailrun stop` to manage it |
| `--name` | `NAME` | auto (`jailrun-<uuid>`) | Assign a human-readable name to the jail (replaces the auto-generated name). Duplicate names are rejected with a clear error |
| `-v`, `--volume` | `HOST:CTR[:ro]` (repeatable) | none | Bind-mount HOST path into the jail at CTR path (nullfs). Append `:ro` for read-only |
| `-e`, `--env` | `KEY=VALUE` (repeatable) | none | Set environment variable inside the jail |
| `-w`, `--workdir` | `DIR` | none | Working directory inside the jail |
| `--timeout` | `SECONDS` | engine default (`DEFAULT_JEXEC_TIMEOUT_S`, 1800s) | Kill the jexec'd command after this many seconds. Real builds (esp-idf/platformio toolchain fetch + compile) can need more |
| `--cpus` | `N` (float) | none | CPU core limit (e.g. `--cpus=2`). Maps to `cputime:sigkill` rctl rule (N × 3600 CPU-seconds). Does NOT merge with `--rctl-rule` |
| `--memory` | `SIZE` | none | Memory limit (e.g. `--memory=4g`, `--memory=512m`). Maps to `memoryuse:sigkill` rctl rule. Overrides the built-in 8g default when used alone; replaces all defaults when combined with `--rctl-rule` |
| `--network` | `none` \| `inherit` \| `vnet` | `none` | `none`: no network inside the jail (ip4/ip6 disabled) — package/toolchain provisioning already happens on the host before the jail is created. `inherit`: opt-in, shares the host's network stack — only for commands that genuinely need registry/network access at exec time. `vnet`: per-jail virtual network stack with dedicated epair(4) interface and private IP — full network isolation |
| `--allow-raw-sockets` | — | off | Allow raw sockets inside the jail (e.g. for ping-like diagnostics). Jails are plain (no VNET), so this is host-network-wide |
| `--no-rctl` | — | rctl **on** by default | Disable rctl resource limits (CPU/memory/process/disk-IO) |
| `--rctl-rule` | `ACTION:VALUE` (repeatable) | built-in defaults | Override the default rctl rule set, e.g. `--rctl-rule pcpu:deny=200 --rctl-rule memoryuse:deny=4g`. Replaces (does not merge with) the built-in defaults |
| `-it` | — | off | Allocate a pseudo-TTY and keep stdin open. Forwards terminal keystrokes to the jailed process and displays its output as if connected directly to the terminal (like `docker run -it`) |
| `--read-only` | — | off | Mount the rootfs dataset read-only. Combine with `--tmpfs` for writable temp directories |
| `--tmpfs` | `PATH[:SIZE]` (repeatable) | none | Mount a tmpfs at `PATH` inside the jail. Optionally append `:SIZE` (e.g. `--tmpfs /tmp:512m`). Default size is 128m. Useful with `--read-only` |

Positional: `IMAGE` (required, e.g. `alpine:3.19` or `esphome/esphome:2025.5`),
then `CMD` (everything after IMAGE, passed straight through as argv inside the
jail).

### Example

This host is Linux, so `run` fails at the very first step (no `skopeo`, no
FreeBSD). That failure is itself the accurate demonstration of the FreeBSD
requirement — this is the real, unedited output:

```
$ python3 -m runtime.cli run --rm alpine:3.19 echo hi
[... Python traceback through runtime/engine.py, store/store.py ...]
store.store.StoreError: Command failed to start: skopeo copy --override-os linux docker://alpine:3.19 oci:/var/cache/jailrun/oci/alpine_3.19:latest: [Errno 2] No such file or directory: 'skopeo'
```

(Reproduce with: `python3 -m runtime.cli run --rm alpine:3.19 echo hi` from the
repo root, on a non-FreeBSD host with no `skopeo` installed.)

On a real FreeBSD 15+ host with `skopeo`/`bsdtar`/ZFS set up (`jailrun doctor`
below tells you exactly what's missing), `run` pulls the image, clones a ZFS
snapshot, spawns the jail, and streams stdout/stderr with the real exit code —
see the README's ESP32 firmware example for what a full successful run looks
like end to end.

---

## Daemon mode — `run -d`

```
jailrun run -d [--name NAME] [FLAGS] IMAGE [CMD [ARGS...]]
```

Runs `CMD` in the background (detached). The jail is created, the command is
exec'd via `jexec` inside a background worker process (`runtime/runner.py`),
and the CLI prints the jail name and returns immediately.

### Example: detached run

```
$ jailrun run -d --name mybuild alpine:3.19 sh -c 'for i in 1 2 3; do echo line $i; sleep 1; done'
mybuild
```

The shell prompt returns instantly. The command runs in the background.

---

## `jailrun ps`

```
jailrun ps [--all]
```

List jailrun-managed runs from the run-state SQLite database. Shows only
`running` rows by default; pass `--all` to include exited/killed history.

### Example

```
$ jailrun ps
JAIL       IMAGE         STATUS    STARTED
mybuild    alpine:3.19   running   2026-07-24T23:19:44+00:00
```

---

## `jailrun logs`

```
jailrun logs JAIL_NAME
```

Print a run's captured stdout/stderr, looked up via the run-state db's
`log_path` column — the same way `docker logs` retrieves a container's output
after the fact.

| Flag | Argument | Default | Description |
|---|---|---|---|
| `-f`, `--follow` | — | off | Follow log output in real time (like `tail -f`). Exits when the jail stops |

### Example

```
$ jailrun logs mybuild
line 1
line 2
line 3
```

---

## `jailrun exec`

```
jailrun exec [FLAGS] JAIL_NAME CMD [ARGS...]
```

Run an additional command inside an already-running jail (docker-exec
semantics). The jail must be live (verified via `jls -n`). Creates or destroys
nothing.

| Flag | Argument | Default | Description |
|---|---|---|---|
| `-e`, `--env` | `KEY=VALUE` (repeatable) | none | Set environment variable for this exec'd command |
| `-w`, `--workdir` | `DIR` | none | Working directory inside the jail for this exec'd command |
| `--timeout` | `SECONDS` | engine default (1800s) | Kill this exec'd command after this many seconds |
| `-d`, `--detach` | — | off | Run the command in background. Output goes to the jail's log file |

### Example

```
$ jailrun exec mybuild echo hello from exec
hello from exec
```

---

## `jailrun stop`

```
jailrun stop [--rm] JAIL_NAME
```

Stop a running jail: SIGKILL all processes (via `jexec kill -KILL -1`), remove
the persist jail (`jail -r`), and update the run-state db to status=`killed`.

| Flag | Argument | Default | Description |
|---|---|---|---|
| `--rm` | — | off | Also destroy the clone/rootfs dataset after stopping |

Without `--rm`, the dataset is preserved for later cleanup via `jailrun rm` or
`jailrun gc --fix`.

### Example

```
$ jailrun stop --rm mybuild
jailrun: stopped mybuild
```

---

## `jailrun rm`

```
jailrun rm JAIL_NAME
```

Destroy the ZFS clone or plaindir copy backing a stopped jail. The jail must
NOT be running (stop it first with `jailrun stop`). Does not modify the
run-state db entry itself.

### Example

```
$ jailrun rm mybuild
jailrun: removed mybuild (jailrun/runs/bb366b37390e433bbfe569da21dbcfaf)
```

---

## `jailrun wait`

```
jailrun wait JAIL_NAME
```

Tail -f of a jail's log file. Reads from the beginning and follows live output
until the jail stops or the user presses Ctrl-C. Like `docker attach` but
read-only (no stdin forwarding).

### Example

```
$ jailrun wait mybuild
line 1
line 2
line 3
```

---

## `jailrun inspect`

```
jailrun inspect [--format text|json] JAIL_NAME
```

Print detailed information about a jail as JSON: run-state (from the rundb),
live status (from `jls -n`), and store backend information — the same shape
as `docker inspect`.

### Example

After the jail has exited (live=false):

```json
$ jailrun inspect mybuild
{
  "jail_name": "mybuild",
  "live": false,
  "rundb": {
    "image": "alpine:3.19",
    "image_digest": "jailrun/images/...",
    "dataset": "jailrun/runs/...",
    "status": "exited",
    "exit_code": 0,
    "started_at": "2026-07-24T23:19:44+00:00",
    "ended_at": "2026-07-24T23:19:47+00:00",
    "log_path": "/var/log/jailrun/mybuild.log"
  }
}
```

JSON format is the default; `--format text` is accepted but currently outputs
the same JSON.

---

## `jailrun explain`

```
jailrun explain (--manifest FILE | IMAGE) [--format text|json]
```

Answers "will this image run under jailrun, how, and what would make it
better" by rendering a substitution manifest: which binaries are native, which
fall back to Linuxulator and why, and the concrete pkg/port fix that would
flip a given binary to native.

Two ways to get a manifest:
- `--manifest FILE` — load a pre-produced JSON manifest from disk. Works on
  any host.
- `IMAGE` — resolve the manifest the same way `jailrun run` does (resolve →
  unpack → clone → probe/bakery). FreeBSD-host only.

### Flags

| Flag | Argument | Default | Description |
|---|---|---|---|
| `--manifest` | `FILE` | none | Load a substitution manifest JSON file from disk instead of resolving IMAGE |
| `--format` | `text` \| `json` | `text` | Output format |

Positional: `IMAGE` (optional — omit if `--manifest` is given). OCI image
reference to resolve a manifest for; FreeBSD-host only.

### Example: `--manifest` (works on any host)

The manifest must validate against
[`schemas/substitution-manifest.schema.json`](../schemas/substitution-manifest.schema.json).
Here's a small synthetic one — one native binary, one linuxulator binary with
an unresolved provider, one linuxulator binary with no provider mapped at all:

```
$ cat > /tmp/example-manifest.json <<'EOF'
{
  "image": "example/demo:1.0",
  "binaries": [
    {
      "path": "/bin/sh",
      "role": "load-bearing",
      "abi": "freebsd",
      "status": "native",
      "native": {
        "provider": "pkg:sh",
        "artifact_path": "/rescue/sh",
        "verification": "exists"
      }
    },
    {
      "path": "/usr/bin/python3",
      "role": "load-bearing",
      "abi": "linux",
      "status": "linuxulator",
      "native": {
        "provider": "pkg:python311",
        "verification": "guessed"
      }
    },
    {
      "path": "/usr/bin/weird-tool",
      "role": "auxiliary",
      "abi": "linux",
      "status": "linuxulator"
    }
  ],
  "linuxulator": {
    "required": true,
    "gaps": ["epoll_pwait2"],
    "risk": "low"
  }
}
EOF
```

Text format (default):

```
$ python3 -m runtime.cli explain --manifest /tmp/example-manifest.json
jailrun explain: example/demo:1.0
========================================

BINARY      ABI      STATUS       PROVIDER       VERIFICATION
-------------------------------------------------------------
sh          freebsd  native       pkg:sh         exists
python3     linux    linuxulator  pkg:python311  guessed
weird-tool  linux    linuxulator  —              —

WHY (linuxulator):
  python3: provider proposed but not resolved
  weird-tool: no native provider mapped

SUMMARY: 1/3 native
Linuxulator required: yes

HINTS (what would make this run more natively):
  - pkg install python311    # resolves python3
  - add a pkg:/port: mapping for weird-tool
```

JSON format:

```
$ python3 -m runtime.cli explain --manifest /tmp/example-manifest.json --format json
{
  "counts": {
    "native": 1,
    "linuxulator": 2,
    "total": 3
  },
  "linuxulator_required": true,
  "binaries": [
    {
      "path": "/bin/sh",
      "abi": "freebsd",
      "status": "native",
      "provider": "pkg:sh",
      "verification": "exists",
      "why": null
    },
    {
      "path": "/usr/bin/python3",
      "abi": "linux",
      "status": "linuxulator",
      "provider": "pkg:python311",
      "verification": "guessed",
      "why": "provider proposed but not resolved"
    },
    {
      "path": "/usr/bin/weird-tool",
      "abi": "linux",
      "status": "linuxulator",
      "provider": null,
      "verification": null,
      "why": "no native provider mapped"
    }
  ]
}
```

Both commands exit `0`.

### The `IMAGE` path needs FreeBSD

Passing an image reference instead of `--manifest` reuses `run`'s own
resolve/unpack/clone pipeline, so it fails the same way on a non-FreeBSD host
— real output from this Linux dev host:

```
$ python3 -m runtime.cli explain notarealimage:latest
[... same resolve() traceback as `run` above ...]
store.store.StoreError: Command failed to start: skopeo copy --override-os linux docker://notarealimage:latest oci:/var/cache/jailrun/oci/notarealimage_latest:latest: [Errno 2] No such file or directory: 'skopeo'
```

---

## `jailrun doctor`

```
jailrun doctor [--format text|json]
```

Inspects the host and reports jailrun readiness: required tools (`skopeo`,
`bsdtar`), kernel modules (`linux64`), FreeBSD-specific config (ZFS pool,
racct, pkg trust keys), and pf anchor hooks for `--network allow`. Prints
exact fix text on failure. Exit code is `0` if every check passed or was
skipped, `1` if any check failed.

### Flags

| Flag | Argument | Default | Description |
|---|---|---|---|
| `--format` | `text` \| `json` | `text` | Output format: `text` (human-readable) or `json` |

### Example

Real output from this (Linux) host — note the FreeBSD-specific checks
correctly `SKIP` instead of crashing:

```
$ python3 -m runtime.cli doctor
[FAIL] os_is_freebsd: platform.system()=Linux
    Fix: jailrun's runtime needs FreeBSD; you can edit/test on any OS but runs require a FreeBSD 15+ host.
[FAIL] skopeo_present: not found in PATH
    Fix: pkg install skopeo
[FAIL] bsdtar_present: not found in PATH
    Fix: bsdtar missing — expected in FreeBSD base system; on other OSes install libarchive-tools.
[SKIP] zpool_present: skipped (not FreeBSD)
[SKIP] racct_enabled: skipped (not FreeBSD)
[SKIP] linux64_loaded: skipped (not FreeBSD)
[SKIP] pkg_trust_keys: skipped (not FreeBSD)
```

Exit code: `1` (some checks failed).

JSON format:

```
$ python3 -m runtime.cli doctor --format json
[
  {
    "name": "os_is_freebsd",
    "status": "fail",
    "detail": "platform.system()=Linux",
    "fix": "jailrun's runtime needs FreeBSD; you can edit/test on any OS but runs require a FreeBSD 15+ host."
  },
  {
    "name": "skopeo_present",
    "status": "fail",
    "detail": "not found in PATH",
    "fix": "pkg install skopeo"
  },
  {
    "name": "bsdtar_present",
    "status": "fail",
    "detail": "not found in PATH",
    "fix": "bsdtar missing — expected in FreeBSD base system; on other OSes install libarchive-tools."
  },
  {
    "name": "zpool_present",
    "status": "skip",
    "detail": "skipped (not FreeBSD)",
    "fix": ""
  },
  {
    "name": "racct_enabled",
    "status": "skip",
    "detail": "skipped (not FreeBSD)",
    "fix": ""
  },
  {
    "name": "linux64_loaded",
    "status": "skip",
    "detail": "skipped (not FreeBSD)",
    "fix": ""
  },
  {
    "name": "pkg_trust_keys",
    "status": "skip",
    "detail": "skipped (not FreeBSD)",
    "fix": ""
  }
]
```

On a properly provisioned FreeBSD 15+ host, `os_is_freebsd`/`skopeo_present`/
`bsdtar_present` should show `[OK]` and the currently-`SKIP`ped checks run for
real instead.

---

## `jailrun version`

```
jailrun version
```

Prints the jailrun version and a one-line description of the runtime/host
split. No flags besides `-h`/`--help`.

### Example

```
$ python3 -m runtime.cli version
jailrun version 0.1.0
runtime: FreeBSD jails + ZFS (freebsd-host only)
host build: linux-host/Linux (design + scaffold)
```

---

## Invoking the CLI

All examples above use `python3 -m runtime.cli ...` from the repo root (works
without installing anything, on any OS with Python 3.10+). If you're on a
checked-out repo with the shim available, `bin/jailrun ...` does the same
thing — it sets `PYTHONPATH` to the repo root and execs
`python3 -m runtime.cli "$@"`, so it works from any cwd, not just the repo
root.

## See also

- [README.md](../README.md) — project overview, architecture summary, the
  esphome/ESP32 end-to-end example
- [docs/DEV_ENVIRONMENT.md](DEV_ENVIRONMENT.md) — setting up a FreeBSD 15+ host
  to actually run images (ZFS pool layout, 9p source delivery, environment
  variables)

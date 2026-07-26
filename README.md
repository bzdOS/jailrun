# jailrun

**A `docker run`-shaped runtime for FreeBSD — backed by jails, not a daemon.**

![status: 1.0-alpha](https://img.shields.io/badge/status-1.0--alpha-yellow)
![license: MIT](https://img.shields.io/badge/license-MIT-blue)
![platform: FreeBSD](https://img.shields.io/badge/platform-FreeBSD%2015-red)
[![CI](https://github.com/bzdOS/jailrun/actions/workflows/ci.yml/badge.svg)](https://github.com/bzdOS/jailrun/actions/workflows/ci.yml)

jailrun runs OCI images on FreeBSD by **substituting native FreeBSD binaries for the
image's Linux ones wherever an equivalent exists**, and falling back to **Linuxulator
only for the irreducible**. The image/layer store is **ZFS** (snapshot = image, clone =
writable layer). No `dockerd`, no Linux VM.

## Why
The container *primitive* is a solved, commodity problem — FreeBSD jails predate
cgroups/namespaces and are a single, kernel-integrated isolation mechanism; ZFS gives
you images and layers for free. Docker's real moat was never the runtime — it's the
**Linux payload** (the world's images are Linux userland) plus the registry/ecosystem.

So jailrun doesn't *emulate* Docker. It returns `docker run`'s functions to native
FreeBSD primitives and **shrinks the Linux-ABI surface to the minimum, one binary at a
time**: run native where we can, use Linuxulator only where we must.

| `docker run …` | jailrun → FreeBSD |
|---|---|
| image (immutable rootfs) | ZFS snapshot, seeded by `pkg`/ports or unpacked OCI layers |
| `pull` / registry | `skopeo` + `bsdtar` (OCI — `umoci` is not in FreeBSD ports), or `pkg` for native bases |
| writable layer | `zfs clone` (copy-on-write) |
| `run -d` / detached | background worker (`runtime/runner.py`) via JSON job file + `subprocess.Popen` |
| `exec` | `jexec(8)` (stream stdio, exact return code) |
| `-v host:ctr` | `mount_nullfs` |
| `--cpus` / `--memory` | `rctl(8)` resource limits |
| `--read-only` / `--tmpfs` | `zfs set readonly=on` / `tmpfs` mount |
| `-it` interactive | `pty.openpty()` + pseudo-terminal forwarding |
| `--network` | `none` (default), `inherit`, `allow=host:port` (pf anchor), `vnet` (epair) |
| `-p HOST:CTR` | pf rdr rules for port forwarding (VNET) |
| `--rm` | `jail -r` + `zfs destroy` |
| `commit` | `zfs snapshot` + `zfs clone` to images tree |
| `cp` | tar over `jexec` |
| `stats` | `rctl -u` |
| `inspect` | JSON from rundb + `jls` |
| `ps` / `logs` / `stop` / `rm` | run-state SQLite db + `jail -r` + `zfs destroy` |
| `freeze` / `thaw` | `jexec kill -STOP/-CONT -1` |

## Status: 1.0-alpha

The core pipeline runs end to end on FreeBSD 15.x: pull an OCI image, resolve
native substitutes, provision a FreeBSD base dataset, and exec the workload
inside a hardened jail — all through a single `jailrun run` command. The full
`docker run`-shaped CLI surface (daemon mode, VNET, commit/cp, stats, `-it`,
resource limits) is in place and tested. It's alpha because three
things a 1.0 needs aren't there yet: most of the provider registry lacks
verification evidence, no external security review has happened, and the
compat matrix covers 9 images, not the hundreds a "will this run" answer
needs to be trustworthy at scale. See [ROADMAP.md](ROADMAP.md) for the exact
bar and what's left to clear it.

Sandbox hardening is verified against deliberately-bad input:
- **Network default-deny** — jails get no network unless `--network inherit` or
  `--network allow=host[:port]` is passed.
- **Destination-scoped egress** — `--network allow=host[:port],...` restricts a
  jail to specific outbound destinations via per-jail pf anchor.
- **Timeouts** — `--timeout` kills a hung command by removing the whole jail.
- **rctl resource limits** — CPU time, memory, process count, disk I/O.
- **OCI-layer symlink-escape** — 2 real bugs found and fixed, 35 adversarial
  layer tests pass, 10 jail-escape techniques all blocked.
- **Extraction jail** — OCI layer parsing runs as `nobody` inside a throwaway
  jail (opt-in: `JAILRUN_EXTRACT_IN_JAIL=1`).
- **Signal resilience** — SIGKILL at any lifecycle stage leaves zero orphans
  after `jailrun gc --fix`.

**Known gaps (tracked for 1.x):** no Capsicum, no VIMAGE/VNET for per-jail
network isolation, no image-signing verification. See
[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) and [ROADMAP.md](ROADMAP.md).

## Tutorial: compile an ESP32 firmware on FreeBSD in 5 minutes

This is the end-to-end proof that motivated jailrun: compile real ESP32
firmware (C++ → ESP-IDF toolchain → cmake/ninja → `.bin`/`.elf`) using
FreeBSD jails, with no Linux VM, no Docker daemon, and no Linux ABI involved
for the load-bearing compiler path.

### Prerequisites

A FreeBSD 15.x host with:
- ZFS pool (default: `jailrun`, overridable via `JAILRUN_ZPOOL`)
- `kern.racct.enable=1` in `/boot/loader.conf` (for rctl)
- `skopeo` installed (`pkg install skopeo`)
- `linux64` kernel module (optional — only needed for binaries that can't be
  substituted natively)

Run `jailrun doctor` to check readiness — it tells you exactly what's missing.

### Step 1: Get jailrun

```sh
# From the repo
git clone https://github.com/bzdOS/jailrun /opt/jailrun
cd /opt/jailrun

# Or from a release zipapp (no git needed)
# fetch https://github.com/bzdOS/jailrun/releases/download/v1.0.0/jailrun.pyz
# alias jailrun='python3 /path/to/jailrun.pyz'
```

### Step 2: Run the ESPHome compiler

```sh
# Create a minimal ESPHome project
mkdir -p /tmp/blink-esp32
cat > /tmp/blink-esp32/blink.yaml <<'EOF'
esphome:
  name: blink-test
esp32:
  board: esp32dev
  framework:
    type: esp-idf
logger:
EOF

# Compile it in a jail (first run pulls the image + provisions native base)
jailrun run \
  --rm \
  --network inherit \
  --timeout 1800 \
  --volume /tmp/blink-esp32:/config \
  --workdir /config \
  esphome/esphome:stable \
  esphome compile blink.yaml
```

**What happens:**
1. `esphome/esphome:stable` is pulled via `skopeo` and unpacked into a ZFS snapshot
2. Each Linux binary is classified: the `xtensa-esp-elf-gcc` toolchain is
   substituted with the native FreeBSD port `devel/xtensa-esp-elf`
3. A FreeBSD base dataset is provisioned with necessary packages
4. A jail is created, the native base is bind-mounted into it, and
   `esphome compile blink.yaml` runs inside — the ESP-IDF framework fetches
   its dependencies (cmake, ninja, Python packages), builds the firmware
5. On success, firmware files appear in `/tmp/blink-esp32/blink-test/.esphome/build/`:
   `firmware.factory.bin`, `firmware.ota.bin`, `firmware.elf`
6. The jail is destroyed (`--rm`)

### Step 3: Verify

```sh
ls -la /tmp/blink-esp32/blink-test/.esphome/build/blink-test/.pioenvs/blink-test/
# Should show: firmware.bin, firmware.elf, firmware.map, etc.
```

### Troubleshooting

- **First run is slow** — it pulls the OCI image (~400 MiB), probes every
  binary, resolves native substitutes, and `pkg install`s the base dataset.
  Subsequent runs use the cache and take <1s to reach the jail.
- **"doctor" check fails** — run `jailrun doctor` and follow its fix
  instructions.
- **Network needed** — ESP-IDF fetches toolchains at compile time. Use
  `--network inherit` (full host network) or `--network allow=<IP>:80,<IP>:443`
  (destination-scoped).
- **Build timeouts** — the first ESP-IDF fetch can take 10+ minutes.
  Increase `--timeout` or set `DEFAULT_JEXEC_TIMEOUT_S` in the environment.
- **No Linux ABI needed** — if you see `kldload linux64`, jailrun is loading
  the Linuxulator only for binaries that could not be substituted (rare for
  esphome after the 0.3 provider backfill). The load-bearing
  `xtensa-esp-elf-gcc` runs as a native FreeBSD ELF.

## How it works — one runtime, four subsystems
See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design and the two contract seams.

- **`runtime/`** — the `jailrun run` CLI + the hybrid exec engine (native-first PATH
  shadowing; enables the Linux ABI only if the manifest requires it).
- **`probe/`** — classifies each binary in an image (ELF `EI_OSABI`) and emits a
  **substitution manifest**; a Linuxulator smoke harness records real syscall gaps.
- **`bakery/`** — the native supply: resolves `pkg:` / `port:` / `build:` providers to
  real FreeBSD artifacts and fills the manifest.
- **`store/`** — the ZFS-native OCI store (the Store API: resolve / unpack / clone /
  mount / destroy).

The two seams everything hangs off: the **Substitution Manifest**
(`schemas/substitution-manifest.schema.json`) and the **Store API**.

## Quickstart (on a FreeBSD 15 host)
> The dev/scaffold lives on any machine; the runtime needs FreeBSD. Run the prove-out
> scripts in order — each is idempotent and self-documenting.

```sh
# 1. storage plumbing: pull -> unpack -> clone -> mount -> destroy (use a glibc image)
sh store/store.freebsd.sh            # e.g. debian:stable-slim

# 2. classify an unpacked rootfs + capture real Linuxulator syscall gaps
python3 probe/probe.py /path/to/rootfs > manifest.json
sh probe/smoke.freebsd.sh

# 3. native supply: pkg/port the substitutes, register a base
sh bakery/bake.freebsd.sh manifest.json

# 4. run it — a native, plain-jail case first (no Linux ABI)
sh runtime/run.freebsd.sh            # then:  python3 -m runtime.cli run --rm IMAGE CMD
```

### Example: compiling real firmware
```sh
jailrun run esphome/esphome:2025.5 compile blink.yaml
```
The ESP toolchain (the load-bearing Linux bit) is substituted by the native
`devel/xtensa-esp-elf` port; everything else runs native-or-Linuxulator, in a
ZFS-cloned jail — a full Linux build tool producing real hardware firmware, with
zero Linux ABI involved.

## Layout
```
ARCHITECTURE.md   design + the two contract seams + cross-seam invariants
ROADMAP.md        known gaps, what's blocked on what, feature ideas
schemas/          substitution-manifest.schema.json (the central contract)
runtime/ probe/ bakery/ store/   the four subsystems (+ a *.freebsd.sh prove-out each)
bin/jailrun       CLI shim
.github/          CI (py_compile + shell syntax + unit tests + schema validation)
```

See [`ROADMAP.md`](ROADMAP.md) for known gaps and what's planned next.

## Relation to bzdOS
jailrun is a sibling of [bzdOS](https://github.com/bzdOS/bsdos) — same FreeBSD,
jails-first worldview. The jail lifecycle (freeze/thaw/kill) is self-contained
(native `jexec kill`), no external daemon required.

## License
MIT — see [LICENSE](LICENSE).

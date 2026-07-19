# jailrun

**A `docker run`-shaped runtime for FreeBSD — backed by jails, not a daemon.**

![status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)
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
| namespaces + cgroups | `jail(2)` (+ VNET) |
| `-v host:ctr` | `mount_nullfs` |
| logs + exit code | `jexec` as a subprocess (stream stdio, exact return code) |
| `--rm` | `jail -r` + `zfs destroy` |

## Status: pre-alpha

The core pipeline runs end to end: `jailrun run esphome/esphome:stable esphome
compile blink.yaml` executes through jailrun's own code (`runtime.cli` →
`runtime.engine` → `store.py`/`probe.py`/`bakery.py`) on a real FreeBSD 15.1 host and
produces a real ESP32 firmware image (`firmware.factory.bin`/`firmware.ota.bin`/
`firmware.elf`) via a genuine ESP-IDF network fetch and cmake/ninja/gcc build.

Sandbox hardening is verified against deliberately-bad input, not just present in the
code:
- **Network default-deny** — jails get no network unless `--network inherit` is
  passed; `ping` fails with "Protocol not supported" by default.
- **Timeouts** — `--timeout` kills a hung command by removing the whole jail, not
  just the wrapper process (SIGKILL to a parent doesn't cascade to its children).
- **rctl resource limits** — a `cputime:sigkill=N` rule kills a CPU-bound busy-loop
  in practice (needs `kern.racct.enable=1`, a loader tunable).
- Two independent OCI-layer symlink-escape bugs are closed with regression tests.

**Known gaps:** no adversarial red-team review yet, no Capsicum, and network
isolation is binary (allow/deny) rather than scoped by destination. See
[CHANGELOG.md](CHANGELOG.md) for the full list of what's fixed and what's open.

**Does native substitution actually save time?** It depends more on host
contention than on the ABI itself, which is itself an interesting finding: on
a busy, shared host the same ESP32 compile ran ~30% faster through the native
`xtensa-esp-elf` substitute than through the image's own toolchain under
Linuxulator — but pinning both arms to dedicated CPU cores (removing
contention) shrank that gap to ~2%, close to noise. Read together, that
suggests native substitution's advantage shows up most under **contention**
(a realistic multi-tenant host), not as a fixed compute-speed win. Against
Docker specifically the native run was roughly comparable either way (not
faster). See [`bench/`](bench/) for the full methodology, both datasets, and
the honest caveats on each.

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
jails-first worldview. It reuses bzdOS's jail-lifecycle machinery (`bsdos_lifecycled`)
for the ephemeral compile-jail lifecycle.

## License
MIT — see [LICENSE](LICENSE).

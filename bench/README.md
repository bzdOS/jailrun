# jailrun benchmark: native substitution vs Linuxulator vs Docker

Quantifies jailrun's core thesis instead of just asserting it: does substituting
native FreeBSD binaries for an image's Linux ones actually save real time on a
real workload, and how does the result compare to just running the same image
under Docker?

## What this measures (and what it doesn't)

Two arms run the **same image, same command, on the same FreeBSD host** —
this isolates the cost of ABI translation (Linuxulator) from anything else
(different hardware, different hypervisor, network variance):

- **native** — the real bakery-resolved manifest: load-bearing binaries are
  shadowed by native FreeBSD substitutes (e.g. the `xtensa-esp-elf` port for
  the ESP toolchain), everything else is whatever probe/bakery classified.
- **linuxulator** — a copy of that *same* manifest with every `status:
  "native"` entry forced to `status: "linuxulator"`. No native shadow symlinks
  get created, so the run falls back to the image's own (Linux) binaries under
  the Linuxulator ABI instead. Same image, same command — only the
  substitution decision changes.

`bench.py` does this by monkeypatching `store.clone()` for the duration of a
run to seed the desired manifest into the fresh clone, then calls
`runtime.engine.run()` completely unmodified — both arms exercise the real
code path, not a simulation of it.

A third arm, **docker**, runs the identical image/command via `docker run` on
the Linux host that hypervises the FreeBSD VM used for the other two arms —
same physical CPU, but **not** the same resource allocation or isolation
(more on that below; this arm is context, not a controlled comparison).

**Explicitly out of scope for this pass** (not silently skipped — noted so
nobody mistakes silence for "doesn't matter"):
- No VM-per-container baseline (e.g. bhyve running the same Linux image) —
  that needs separate nested-virtualization infra to stand up and wasn't
  built for this pass.
- No density/concurrent-instance test.
- Single run for the real-workload arm on each side (compiling is slow;
  cold-start has 5 reps for a median/p95, the compile does not) — see
  "Unpinned vs pinned" below for why this matters more than it sounds like it
  should.
- Not run on fully dedicated hardware. A `virsh vcpupin` experiment (below)
  shows this actually changes the headline number, not just the noise floor —
  a real dedicated benchmark machine is still the right next step before
  trusting either number to more than one significant figure. **Planned:** a
  dedicated single-core benchmark box — trades away parallel-build realism for
  zero scheduling variance by construction (nothing to contend for, nothing to
  pin), which is worth more than realism for isolating this specific
  native-vs-Linuxulator question.

## Hosts

- **FreeBSD arms (native/linuxulator):** FreeBSD 15.1-RC2 libvirt guest, Intel
  Core i5-14500 (16 vCPU allocated), ~8GB RAM, ZFS store backend.
- **Docker arm:** the Linux host that hypervises that same VM — same
  physical Core i5-14500, 20 threads / 62GB total, but **not isolated**: at
  benchmark time it was concurrently running ~12 other libvirt VMs (load
  average ~5), while the FreeBSD guest's 16 vCPU allocation is comparatively
  more isolated. Same silicon, different contention — treat the Docker number
  as a sanity check, not a precise head-to-head.

## Results (2026-07-19)

Image: `esphome/esphome:stable`. Manifest: 348/1112 binaries classified
native (the rest are things like the many Debian/perl utility scripts bundled
in the image that nobody's mapped a FreeBSD provider for yet — see
`CONTRIBUTING.md`).

| Measurement | native (jail) | linuxulator (jail) | docker (Linux host) |
|---|---|---|---|
| Cold start (trivial command, `--rm`, n=5) | 1.645s median | 1.644s median | not measured |
| Real compile — unpinned, shared host (n=1) | 144.1s | 206.7s | 137.5s |
| Real compile — **pinned**, dedicated cores (n=1) | 151.2s | **153.6s** | not re-measured |

Raw data: [`results/run-1784470462.json`](results/run-1784470462.json) (unpinned),
[`results/run-1784477275.json`](results/run-1784477275.json) (pinned).

### Unpinned vs pinned — the gap mostly wasn't about the ABI

The FreeBSD host runs ~12 other libvirt VMs concurrently (load average ~5 at
benchmark time), and the VM's 16 vCPUs floated freely across all 20 host
threads rather than being reserved. Pinning those 16 vCPUs (and the qemu
emulator thread) to a dedicated set of host cores for the duration of one
re-run — live, via `virsh vcpupin`/`virsh emulatorpin`, no VM restart, reverted
immediately after — changed the result substantially:

- **linuxulator dropped from 206.7s to 153.6s** (~26% faster) — a big move.
- **native barely moved: 144.1s → 151.2s** (if anything slightly slower,
  within single-run noise).
- **The native/linuxulator gap nearly disappeared**: 151.2s vs 153.6s, ~1.6%,
  indistinguishable from noise at n=1.

Read plainly: most of the ~30% gap in the unpinned run wasn't "Linuxulator is
inherently ~30% slower at compiling" — it was Linuxulator being **more
sensitive to CPU scheduling contention** than the native path (more syscalls,
more context switches, more places for a noisy neighbor to interrupt at a bad
moment). Take away the contention and the gap mostly evaporates, at least for
this one build.

This is still just two single-sample data points, not a distribution — the
honest conclusion is a *range* (roughly 2%–30% depending on host contention),
not a single number, and more repetitions on genuinely dedicated hardware are
needed before either end of that range should be trusted. It also flips the
practical framing: the native-substitution advantage may show up most on
**noisy, multi-tenant hosts** (contention-resilience) rather than on quiet,
dedicated ones (raw compute) — which is arguably the more realistic
deployment case, but is a hypothesis this pass can suggest, not confirm.

### Other honest notes

- **native vs Docker (unpinned):** jailrun-native (144.1s) was *not* faster
  than Docker on this run (137.5s) — Docker was about 5% quicker. Given the
  hardware/contention differences (see Hosts, above), that's "roughly
  comparable," not a clear win either way. The claim this benchmark supports
  is: **native substitution in a jail gets you Docker-competitive build times
  while adding a real FreeBSD-native security boundary** (see the
  sandbox-hardening list in the main README) — not "jailrun is faster than
  Docker." Docker wasn't re-measured under the pinned configuration (pinning
  only applies to the FreeBSD guest).
- **Cold start (jail arms only) is a wash** on this host specifically because
  the `linux64` kernel module was already loaded from earlier work — the
  one-time `kldload` cost was already paid before this benchmark ran. What's
  left is just the per-jail Linuxulator mount setup (`linprocfs`/`linsysfs`/
  `tmpfs`/`fdescfs`), which is cheap. A host where `linux64` isn't already
  resident would show a larger native/linuxulator gap here. Docker cold-start
  wasn't measured in this pass.
- Both FreeBSD arms needed `--network inherit`: ESPHome's own first-run fetch
  of the ESP-IDF framework needs network access, which jailrun blocks by
  default — that's the security feature working as documented, not a
  benchmark artifact. (An earlier attempt without the network flag failed
  exactly this way for both arms — same fast failure, not a real signal, so
  those results aren't included here.)
- `zfs destroy` on teardown logged a transient "dataset is busy" for the
  Linuxulator run in both the unpinned and pinned passes (retried and
  succeeded — see `store.py`'s destroy retry logic) — noted here because it's
  a real, repeated observation from running a real build workload, not
  because it affected the timing above.

## Reproducing

FreeBSD arms need a real FreeBSD 15+ host with jailrun's ZFS pool and the
`xtensa-esp-elf` port already provisioned (see `docs/DEV_ENVIRONMENT.md`):

```sh
PYTHONPATH=. python3 bench/bench.py            # full run (~5 min)
PYTHONPATH=. python3 bench/bench.py --quick    # cold-start only, skips the compile
```

Docker arm (any Linux host with Docker):

```sh
mkdir -p /tmp/docker-bench-esphome && cp bench/blink.yaml /tmp/docker-bench-esphome/
docker pull esphome/esphome:stable
time docker run --rm -v /tmp/docker-bench-esphome:/config esphome/esphome:stable compile blink.yaml
```

To reproduce the pinned run (on the libvirt host, not inside the guest) — reserves
16 host cores exclusively for the VM for the duration, then releases them:

```sh
for i in $(seq 0 15); do virsh vcpupin <vm-name> "$i" "$i" --live; done
virsh emulatorpin <vm-name> 0-15 --live
# ... run bench.py inside the guest ...
for i in $(seq 0 15); do virsh vcpupin <vm-name> "$i" 0-19 --live; done   # restore
virsh emulatorpin <vm-name> 0-19 --live                                   # restore
```

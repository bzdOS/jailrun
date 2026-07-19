# probe/ — jailrun S2: compat intelligence

## What this subsystem does

`probe.py` walks an unpacked OCI rootfs directory and emits a **Substitution
Manifest** conforming to `schemas/substitution-manifest.schema.json`.  The
manifest is the central contract: bakery (S4) fills `native.artifact_path`;
runtime (S1) decides per-exec whether to shadow the image binary with a native
FreeBSD one or to run it under Linuxulator.

---

## Classification method

### 1. ELF OSABI detection

Every regular executable file is opened and the first 20 bytes are read to
parse the ELF identity header.  The key byte is `EI_OSABI` at offset 7:

| `EI_OSABI` | Hex | `abi` assigned | Source |
|---|---|---|---|
| `ELFOSABI_NONE` / `ELFOSABI_SYSV` | `0x00` | `linux` | Convention: virtually all Linux distro ELFs stamp 0; kernel ignores the field |
| `ELFOSABI_GNU` / `ELFOSABI_LINUX` | `0x03` | `linux` | GNU-branded ELF (glibc-linked) |
| `ELFOSABI_FREEBSD` | `0x09` | `freebsd` | FreeBSD brand; FreeBSD's `brandelf` stamps this |
| `ELFOSABI_NETBSD` | `0x02` | `unknown` | Not Linux, not FreeBSD |
| anything else | — | `unknown` | Solaris, HP-UX, etc. |

The `e_machine` field (bytes 18–19, respecting `EI_DATA` byte order) is also
recorded and stored in the `notes` field for human inspection.

References:
- ELF spec: [elf(5) — Linux man page](https://man7.org/linux/man-pages/man5/elf.5.html)
- FreeBSD ELF branding: [elf(5) — FreeBSD man page](https://man.freebsd.org/cgi/man.cgi?elf%285%29=)

### 2. Shebang detection

If the file is not an ELF (magic does not match `\x7fELF`), the first 512
bytes are read looking for a `#!` line.  If found: `abi = "script"`, and the
interpreter path is stored in `notes`.

### 3. Role heuristic

| Condition | `role` |
|---|---|
| Script (`abi="script"`) | `auxiliary` |
| Basename in hardcoded `LOAD_BEARING_NAMES` set | `load-bearing` |
| Basename starts with cross-compiler prefix (`xtensa-`, `riscv32-esp-`, `arm-`, …) | `load-bearing` |
| Basename present in provider map | `load-bearing` |
| Native ELF, file size ≥ 1 MiB | `load-bearing` |
| Otherwise | `auxiliary` |

### 4. Status assignment

| `abi` | provider in map? | `status` |
|---|---|---|
| `freebsd` | — | `native` (trivially; already the right ABI) |
| `linux` | yes | `native` (candidate; bakery fills `artifact_path`) |
| `linux` | no | `linuxulator` |
| `script` | — | `native` (scripts are portable) |
| `unknown` | — | `unknown` |

### 5. Linuxulator block

After all binaries are classified:

- `required = true` if any binary has `status = linuxulator`
- `gaps` is initially empty; filled by `smoke.freebsd.sh` after freebsd-host validation
- `risk` heuristic:
  - `none` — no linuxulator binaries
  - `low` — 1–2 auxiliary binaries staying under Linuxulator
  - `high` — any load-bearing binary staying under Linuxulator
  - `medium` — otherwise

---

## Linux → FreeBSD provider map

The map is the `PROVIDER_MAP` dict in `probe.py`.  It maps **binary basename**
(case-insensitive) to a FreeBSD provider string with one of three schemes:

| Scheme | Meaning | Example |
|---|---|---|
| `pkg:<name>` | Install from binary package repo | `pkg:python311` |
| `port:<origin>` | Build from ports tree | `port:devel/xtensa-esp-elf` |
| `build:<recipe-id>` | Custom S4 bakery build recipe | `build:openocd-esp` |

### Current map (abridged)

| Linux binary | FreeBSD provider |
|---|---|
| `python3`, `python3.11` | `pkg:python311` |
| `python3.10` | `pkg:python310` |
| `python3.12` | `pkg:python312` |
| `cmake` | `pkg:cmake` |
| `ninja` | `pkg:ninja` |
| `make`, `gmake` | `pkg:gmake` |
| `meson` | `pkg:meson` |
| `gcc`, `g++` | `pkg:gcc` |
| `clang`, `clang++` | `pkg:llvm` |
| `ld`, `ar`, `objcopy`, `strip`, `nm`, `readelf` | `pkg:binutils` |
| `xtensa-esp32-elf-gcc` | `port:devel/xtensa-esp-elf` |
| `xtensa-esp32-elf-g++` | `port:devel/xtensa-esp-elf` |
| `xtensa-esp32s2-elf-gcc` | `port:devel/xtensa-esp-elf` |
| `xtensa-esp32s3-elf-gcc` | `port:devel/xtensa-esp-elf` |
| `xtensa-lx106-elf-gcc` | `port:devel/xtensa-esp-elf` |
| `riscv32-esp-elf-gcc` | `port:devel/riscv32-esp-elf` |
| `node`, `nodejs` | `pkg:node` |
| `ruby` | `pkg:ruby` |
| `perl`, `perl5` | `pkg:perl5` |
| `java` | `pkg:openjdk21` |
| `bash`, `sh` | `pkg:bash` |
| `curl` | `pkg:curl` |
| `git` | `pkg:git` |
| `openssl` | `pkg:openssl` |
| `esphome` | `pkg:py311-esphome` |
| `pip3`, `pip` | `pkg:py311-pip` |

**Extending the map**: add a row to `PROVIDER_MAP` in `probe.py`.  The key is
the binary basename (lower-case); the value follows the `scheme:identifier`
convention.  For cross-compiler families where many variants share one port,
add each variant explicitly — the mapping is a simple dict lookup, not a glob.

---

## Linuxulator smoke testing (freebsd-host only)

`smoke.freebsd.sh` runs each `status=linuxulator` binary inside a transient
FreeBSD jail with Linuxulator enabled, under `truss(1)` and `ktrace(1)`, to
harvest `ENOSYS` (errno 78 on Linux = "Function not implemented") returns.

### How truss/ktrace catches ENOSYS

When a Linux binary calls a syscall that the Linuxulator has registered as
`UNIMPL` (via the `DUMMY` macro in `sys/amd64/linux/linux_dummy.c`), the
kernel substitutes `nosys`, which:
1. Logs a message: `"syscall <name> not implemented"`
2. Returns `ENOSYS` to the caller

`truss -f` traces the child process tree and decodes return values; grep for
`ENOSYS` or `ERR#78` extracts the unimplemented call names.  `ktrace -t C`
records system-call events at the kernel boundary; `kdump` decodes them.

References:
- [truss(1)](https://man.freebsd.org/cgi/man.cgi?query=truss)
- [ktrace(1)](https://man.freebsd.org/cgi/man.cgi?query=ktrace)
- [FreeBSD Linuxulator wiki](https://wiki-dev.freebsd.org/Linuxulator)

### Known Linuxulator gaps (FreeBSD 13–14.x)

| Syscall | Status | Bug |
|---|---|---|
| `signalfd(2)` | Unimplemented | [Bug 285881](https://www.mail-archive.com/freebsd-bugs@freebsd.org/msg89107.html) |
| `inotify_init(2)` | Unimplemented | [Bug 240874](https://bugs.freebsd.org/bugzilla/show_bug.cgi?id=240874) |
| `inotify_init1(2)` | Unimplemented | same |
| `inotify_add_watch(2)` | Unimplemented | same |
| `io_uring_setup(2)` | Not implemented | — |
| `io_uring_enter(2)` | Not implemented | — |
| `io_uring_register(2)` | Not implemented | — |
| `fanotify_init(2)` | Not implemented | — |
| `userfaultfd(2)` | Not implemented | — |
| `pidfd_open(2)` | Partial / unverified | — |
| `memfd_create(2)` | Partial | — |
| `clone3(2)` | Partial (old `clone` works for threads) | — |

`epoll` is mapped to `kqueue` internally and generally works.

---

## Compat matrix: "does it run on FreeBSD?"

By collecting manifests across many images, S2 can generate a public matrix:

```
image                          | linuxulator_required | risk   | gaps
-------------------------------|---------------------|--------|-------------------------------
esphome/esphome:2025.5         | false               | none   | []
node:20-alpine                 | true                | low    | []
ubuntu:22.04 + inotify tool    | true                | high   | [inotify_init, inotify_init1]
```

Generation:
1. Run `probe.py` against each image's unpacked rootfs → manifest.
2. Run `smoke.freebsd.sh` on freebsd-host to fill `gaps` in each manifest.
3. Aggregate: `linuxulator.required`, `linuxulator.risk`, `linuxulator.gaps`
   across manifests → a JSON or Markdown table.
4. A binary's `status=native` means Linuxulator was successfully avoided;
   `status=linuxulator` with non-empty `syscalls_needed` signals a
   compatibility risk.

The matrix is continuously updatable: re-run probe + smoke whenever an image
tag changes or a Linuxulator improvement lands in FreeBSD.

---

## Files

| File | Description |
|---|---|
| `probe.py` | Main classifier; run on any host (pure Python ELF parsing) |
| `test_classify.py` | Unit tests for ELF classifier; `python3 test_classify.py` |
| `smoke.freebsd.sh` | Linuxulator smoke harness; **freebsd-host only** — `# UNVERIFIED` |
| `README.md` | This file |

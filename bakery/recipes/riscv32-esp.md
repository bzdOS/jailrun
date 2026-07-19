# Recipe: riscv32-esp-elf (ESP32-C3 / C6 / P4)

**Provider:** `build:riscv32-esp-elf`
**Status:** STUB — no native FreeBSD port or binary package exists (2026-06)
**Target chips:** ESP32-C3, ESP32-C6, ESP32-P4 (RISC-V rv32imc)

## Summary

Espressif uses a custom RISC-V toolchain (`riscv32-esp-elf`) with:
- Target: `riscv32-esp-elf` (rv32imc multilib, Espressif newlib patches)
- GCC 13+ (bundled in ESP-IDF 5.x toolchain download)

As of 2026-06, there is **no FreeBSD port** for this toolchain in either
the official ports tree or known unofficial ports (trombik maintains only
xtensa variants). The `devel/riscv-gnu-toolchain` FreeBSD port targets
generic rv64 and is not compatible.

## Current status and options

| Option | Feasibility | Notes |
|--------|-------------|-------|
| FreeBSD port (official) | Not available | No port exists |
| FreeBSD port (unofficial) | Not known | None found as of 2026-06 |
| Build from source (riscv-gnu-toolchain + Espressif patches) | Unverified | See below |
| Linuxulator + Espressif Linux binary | Works today | Recommended fallback |

## Recommended fallback: Linuxulator

Set `status=linuxulator` in the manifest for any binary resolved by
`build:riscv32-esp-elf`. The Espressif-provided Linux binary (`riscv32-esp-elf-gcc`)
runs under FreeBSD's Linuxulator (Linux ELF ABI compatibility).

Espressif provides pre-built Linux/amd64 binaries in the IDF tools download:

```sh
# On freebsd-host (FreeBSD with Linuxulator enabled)
. $IDF_PATH/export.sh   # downloads riscv32-esp-elf for linux/amd64
# The binary runs via Linuxulator transparently
riscv32-esp-elf-gcc --version
```

This is the "irreducible Linuxulator" case the jailrun architecture
anticipates — exactly what the `linuxulator.required=true` flag is for.

## Stub build path (unverified, for future work)

If a native build is ever needed:

```sh
# Prerequisites
pkg install -y devel/gmake devel/gawk textproc/gsed devel/bison \
    devel/automake devel/libtool devel/git python311 texinfo

# Clone riscv-gnu-toolchain with Espressif newlib patches
git clone https://github.com/espressif/riscv-gnu-toolchain.git \
    --depth 1 --branch esp-2024r1 /opt/riscv-esp

cd /opt/riscv-esp
git submodule update --init --recursive

# Configure for rv32imc (Espressif target)
# UNVERIFIED: configure flags for FreeBSD host
./configure \
    --prefix=/usr/local/riscv32-esp-elf \
    --with-arch=rv32imc \
    --with-abi=ilp32 \
    --with-multilib-generator="rv32imc-ilp32--"

gmake -j$(sysctl -n hw.ncpu)
```

**Expected artifact path (if build succeeds):**
```
/usr/local/riscv32-esp-elf/bin/riscv32-esp-elf-gcc
```

This is completely unverified on FreeBSD and should not be relied upon.

## Caveats

- **Espressif patches required:** Generic riscv-gnu-toolchain does not include
  Espressif's newlib patches (custom syscall stubs, memory layout for ESP32-Cx).
  Using the unpatched toolchain will produce silently wrong binaries.
- **Build host ABI:** FreeBSD is not a tested host for Espressif's toolchain
  build scripts. Expect gmake/gsed/gawk substitutions as with lx106.
- **Long-tail priority:** ESP32-C3/C6 support in esphome/jailrun is lower
  priority than ESP32 and ESP8266. Linuxulator for the RISC-V toolchain is
  acceptable until a proper port emerges.
- **Monitoring:** Watch https://www.freshports.org/search.cgi?query=riscv32-esp
  for a future port submission.

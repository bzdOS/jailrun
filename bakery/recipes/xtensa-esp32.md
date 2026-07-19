# Recipe: xtensa-esp-elf (ESP32 / ESP32-S2 / ESP32-S3)

**Provider:** `port:devel/xtensa-esp-elf`
**Status:** ready — official FreeBSD port (in ports tree, maintained)
**Maintainer:** leres@FreeBSD.org
**Port added:** 2024-09-23 | **Last update:** 2026-06-04
**Version:** 13.2.0.20240530_17

Source: [FreshPorts devel/xtensa-esp-elf](https://www.freshports.org/devel/xtensa-esp-elf/)

## How to obtain natively on FreeBSD

### Via pkg (binary package — fastest)
```sh
pkg install xtensa-esp-elf
```

### Via ports (if pkg binary lags)
```sh
cd /usr/ports/devel/xtensa-esp-elf
make install clean BATCH=yes
```

## What it covers

GCC 13.2.0-based cross-compiler for:
- **ESP32** (Xtensa LX6)
- **ESP32-S2** (Xtensa LX7)
- **ESP32-S3** (Xtensa LX7)
- Compatible with ESP-IDF 5.2.x and 5.3.x

Does NOT cover:
- ESP8266 (lx106) — see `xtensa-lx106.md`
- ESP32-C3/C6/P4 (RISC-V) — see `riscv32-esp.md`

## Artifact paths

The port installs under `/usr/local/xtensa-esp-elf/` (not in `/usr/local/bin`):

```
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-gcc
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-g++
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-ld
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-objcopy
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-objdump
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-ar
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-strip
/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-nm
# ... 2205 files total (compiler, libs, headers, binutils)
```

The bakery resolves `port:devel/xtensa-esp-elf` →
`/usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-gcc` as the representative
artifact path. Runtime (S1) adds `/usr/local/xtensa-esp-elf/bin` to PATH
inside the jail.

## ESP-IDF wiring

ESP-IDF's `idf_tools.py` validates toolchains by crosstool-NG build tag. A
FreeBSD-built binary reports tag `crosstool-NG UNKNOWN` (build host not in
Espressif's allowed list), which causes `idf.py` to warn or error.

**Bypass — put the toolchain on PATH and skip the tool manager:**
```sh
export IDF_PATH=/path/to/esp-idf
export PATH=/usr/local/xtensa-esp-elf/bin:$PATH
export ESP_IDF_VERSION=5.3          # suppress some version checks
# Do NOT run: . $IDF_PATH/export.sh  (that triggers idf_tools.py download)
# Instead run idf.py directly:
idf.py build
```

The `ESP_IDF_VERSION` env var satisfies components that call
`idf_component_manager` version checks. The tool manager download is
completely bypassed when the binaries are already on PATH.

Source: [ESP-IDF idf-tools docs](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/tools/idf-tools.html)
— "although the methods above are recommended … they are not a must"

## Caveats

- **IDF version lock:** GCC 13.2 + this port targets IDF 5.2–5.3. Esphome
  2025.5 uses IDF ~5.3; verify with `idf.py --version` in container image.
- **Flavored legacy port:** For IDF < 5.2 use `devel/xtensa-esp32-elf` with
  flavor (`idf52`/`idf53`). Artifact path then:
  `/usr/local/xtensa-esp32-elf-idf52/bin/xtensa-esp32-elf-gcc`.
- **RISC-V gap:** esp32-c3/c6 use `riscv32-esp-elf`; this port does not
  cover them.
- **PlatformIO symlink:** PlatformIO looks in `~/.platformio/packages/`.
  Create symlink: `ln -s /usr/local/xtensa-esp-elf ~/.platformio/packages/toolchain-xtensa-esp-elf`

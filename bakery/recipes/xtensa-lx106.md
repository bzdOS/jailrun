# Recipe: xtensa-lx106-elf (ESP8266)

**Provider:** `build:xtensa-lx106-elf`
**Status:** experimental — no official FreeBSD port in tree; two build paths
**Target chip:** ESP8266 (Xtensa LX106 core)

## Summary

The ESP8266 toolchain (`xtensa-lx106-elf-gcc` et al.) is NOT in the official
FreeBSD ports tree as of 2026-06. Two practical paths exist:

| Path | Source | Status |
|------|--------|--------|
| A: `devel/esp-quick-toolchain` | trombik unofficial port, GCC 10 | Last release 2022-01-29; experimental |
| B: jcmvbkbc/crosstool-NG lx106 | Build from source on freebsd-host | Experimental (ct-ng BSD support "experimental") |

The bakery **prefers Path A** (the port) because it is packaged and avoids
the uncertain crosstool-NG FreeBSD build. If the port is unavailable or
produces a broken toolchain, fall back to Path B.

## Path A: devel/esp-quick-toolchain (preferred)

Source: [trombik/freebsd-ports-esp-quick-toolchain](https://github.com/trombik/freebsd-ports-esp-quick-toolchain)

```sh
# On freebsd-host: clone the unofficial ports repo and build
git clone https://github.com/trombik/freebsd-ports-esp-quick-toolchain.git \
    /usr/ports/distfiles/esp-quick-toolchain-ports
cp -r /usr/ports/distfiles/esp-quick-toolchain-ports/devel/esp-quick-toolchain \
    /usr/ports/devel/esp-quick-toolchain

cd /usr/ports/devel/esp-quick-toolchain
make install clean BATCH=yes
```

**Artifact paths after install:**
```
/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-gcc
/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-g++
/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-ld
# ... full binutils + compiler suite
```

The `gcc103` flavor means GCC 10.3. The install prefix is
`/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/`.

## Path B: jcmvbkbc/crosstool-NG lx106 branch (fallback)

Source: [jcmvbkbc/crosstool-NG](https://github.com/jcmvbkbc/crosstool-NG) (lx106 branch)

The Xtensa LX106 architecture is not in mainline crosstool-NG (too vendor-specific).
jcmvbkbc maintains a fork with lx106 support.

### FreeBSD prerequisites

From [crosstool-NG OS Setup docs](https://crosstool-ng.github.io/docs/os-setup/):

```sh
pkg install -y \
    archivers/zip devel/automake devel/bison devel/gettext-tools \
    devel/git devel/gmake devel/gperf devel/libatomic_ops \
    devel/libtool devel/patch lang/gawk misc/help2man \
    print/texinfo textproc/asciidoc textproc/gsed textproc/xmlto
```

### Build steps (UNVERIFIED on FreeBSD)

```sh
# Clone lx106 fork
git clone --depth 1 --branch lx106 \
    https://github.com/jcmvbkbc/crosstool-NG.git /opt/crosstool-ng-lx106
cd /opt/crosstool-ng-lx106

./bootstrap

# FreeBSD requires GNU tool overrides
./configure \
    --prefix=/opt/crosstool-ng-lx106/inst \
    --with-sed=/usr/local/bin/gsed \
    --with-make=/usr/local/bin/gmake \
    --with-patch=/usr/local/bin/gpatch

gmake && gmake install
export PATH="/opt/crosstool-ng-lx106/inst/bin:$PATH"

# Select lx106 configuration
ct-ng xtensa-lx106-elf

# Override install prefix in .config
echo 'CT_PREFIX_DIR="/usr/local/xtensa-lx106-elf"' >> .config

# Build (takes 20-60 min; uses all cores)
ct-ng build.$(sysctl -n hw.ncpu)
```

**Artifact path after build:**
```
/usr/local/xtensa-lx106-elf/bin/xtensa-lx106-elf-gcc
```

### Known issues on FreeBSD

- `m4` must be GNU m4 (`pkg install m4` — FreeBSD m4 may not work).
- `help2man` must be on PATH during build.
- crosstool-NG explicitly marks FreeBSD support as "experimental" and notes
  "some samples are failing to build."
- The build succeeded at least once (PlatformIO forum, ~2015, user `sticilface`)
  but required multiple iterations to get g++ included. No documented 2024+ run.

## ESP8266 SDK / IDF notes

The ESP8266 uses the **RTOS SDK v3.x** (not mainline ESP-IDF). It is a frozen
SDK; Espressif declared the ESP8266 end-of-life for new development.

```sh
export IDF_PATH=/path/to/esp8266-rtos-sdk
export PATH=/usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin:$PATH
idf.py build   # or make in legacy projects
```

esphome uses the Arduino/ESP8266 stack for ESP8266 targets, not RTOS SDK.
The relevant gcc invocation is `xtensa-lx106-elf-g++` from PlatformIO's
`toolchain-xtensa` package — which is exactly what esp-quick-toolchain provides.

## Caveats

- **GCC version mismatch:** esp-quick-toolchain provides GCC 10.3. PlatformIO's
  Linux toolchain-xtensa package uses GCC 10.x as well — ABI compatible.
- **Last release 2022:** `devel/esp-quick-toolchain` has not been updated since
  2022-01-29. If the port fails to build (broken distinfo/fetch), the ct-ng
  path is the only option.
- **No pkg binary:** neither path is in pkg.FreeBSD.org, so `pkg install` will
  not work — must build from port or source.
- **Alternative — Linuxulator:** If both build paths fail on freebsd-host, mark
  `status=linuxulator` for ESP8266 targets and run PlatformIO's Linux
  `toolchain-xtensa` binary under Linuxulator. Lower priority because
  Linuxulator increases attack surface.

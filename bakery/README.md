# bakery/ — S4: native supply

Bakery consumes a **Substitution Manifest** (produced by probe, S2) and
resolves every `status=native` binary to a real FreeBSD artifact:
fills `native.artifact_path`, produces a provisioning plan, and registers
a native base with S3 (`store.register_base`).

## Provider → artifact resolution model

A `native.provider` field encodes HOW to obtain a binary on FreeBSD.
The grammar has three prefixes:

```
pkg:<name>         Binary package from pkg.FreeBSD.org
port:<origin>      Build from /usr/ports/<origin>
build:<recipe-id>  From-source build (crosstool-NG, etc.)
```

### pkg:

Fast path. The package manager installs a pre-built binary.
Artifact path is deterministic and well-known (almost always under `/usr/local/bin/`).

```
pkg:python311   →  /usr/local/bin/python3.11
pkg:cmake       →  /usr/local/bin/cmake
pkg:ninja       →  /usr/local/bin/ninja
```

bakery.py maintains `PKG_ARTIFACTS` — a table of known pkg names to paths.
Unknown names fall back to `/usr/local/bin/<name>` with a warning.

### port:

Ports install into custom prefixes. Artifact path must be known ahead of time
(verified on freebsd-host) and is stored in `PORT_ARTIFACTS` in bakery.py.

```
port:devel/xtensa-esp-elf         →  /usr/local/xtensa-esp-elf/bin/xtensa-esp-elf-gcc
port:devel/xtensa-esp32-elf       →  /usr/local/xtensa-esp32-elf-idf52/bin/xtensa-esp32-elf-gcc
port:devel/esp-quick-toolchain    →  /usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-gcc
```

Port builds are triggered by `bake.freebsd.sh` on freebsd-host (not on linux-host).

### build:

From-source recipes for toolchains that have no pkg/port equivalent.
bakery.py consults `RECIPE_REGISTRY` which maps recipe-id → `BuildRecipe`.
A recipe declares its pkg and port dependencies, the final artifact path,
and build status (`ready` / `experimental` / `stub`).

```
build:xtensa-lx106-elf   →  /usr/local/esp-quick-toolchain-gcc103/xtensa-lx106-elf/bin/xtensa-lx106-elf-gcc
build:riscv32-esp-elf    →  STUB (no native path yet; use Linuxulator)
```

## Provisioning plan

`bakery.bake(manifest)` assembles an **ordered provisioning plan**:

1. All `pkg` steps (parallel-safe; pkg resolves deps internally)
2. All `port` steps (sequential; some ports depend on each other)
3. All `build` steps (sequential; may depend on ports above)

Within each kind, steps are deduplicated. The plan is hashed and used as
the content-address key for `store.register_base(name, provision)`.

`bake.freebsd.sh` executes this plan on freebsd-host against the live pkg/ports tree.

## Linux→FreeBSD coverage philosophy

jailrun's thesis is: **shrink the Linux-ABI surface to the minimum, per binary**.

The bakery implements this as a coverage matrix:

| Binary role | Preferred path | Fallback |
|-------------|---------------|---------|
| Python interpreter | `pkg:python3xx` | — (always available) |
| Build tools (cmake, ninja, gmake) | `pkg:*` | — |
| ESP32 toolchain | `port:devel/xtensa-esp-elf` | — (official port) |
| ESP8266 toolchain | `build:xtensa-lx106-elf` (via esp-quick-toolchain) | Linuxulator |
| ESP32-C3/C6 RISC-V toolchain | `build:riscv32-esp-elf` (STUB) | Linuxulator |
| Anything else | Linuxulator | `status=missing` |

The principle: **do not use Linuxulator for a binary if a native FreeBSD
equivalent exists that is production-quality** (maintained pkg/port with
known artifact path). Experimental build paths are acceptable for toolchains
where Linuxulator is the only alternative — a broken native build degrades to
the same Linuxulator path with a warning.

## Recipe coverage

| Recipe file | Provider | Ready? | Notes |
|-------------|----------|--------|-------|
| `python.md` | `pkg:python311` | Yes — official pkg | Standard; always available |
| `cmake-ninja.md` | `pkg:cmake`, `pkg:ninja` | Yes — official pkg | ESP-IDF 5.x ready |
| `xtensa-esp32.md` | `port:devel/xtensa-esp-elf` | Yes — official port | GCC 13, IDF 5.2/5.3; in ports tree since 2024-09 |
| `xtensa-lx106.md` | `build:xtensa-lx106-elf` | Experimental | Unofficial port (2022) or ct-ng fallback |
| `riscv32-esp.md` | `build:riscv32-esp-elf` | STUB | No native path; Linuxulator recommended |

## File layout

```
bakery/
  bakery.py          — main Python module (linux-host-safe; py_compile clean)
  bake.freebsd.sh       — freebsd-host-only execution script (pkg/port/build runner)
  README.md          — this file
  recipes/
    python.md        — pkg:python3xx
    cmake-ninja.md   — pkg:cmake + pkg:ninja
    xtensa-esp32.md  — port:devel/xtensa-esp-elf (ESP32, ready)
    xtensa-lx106.md  — build:xtensa-lx106-elf (ESP8266, experimental)
    riscv32-esp.md   — build:riscv32-esp-elf (ESP32-C3/C6, stub)
```

## Usage (linux-host)

```python
import json
from bakery.bakery import bake

with open("manifest.json") as f:
    manifest = json.load(f)

updated = bake(manifest)   # fills artifact_path, registers base (mocked)
print(json.dumps(updated, indent=2))
```

Or via CLI:
```sh
python3 bakery/bakery.py manifest.json --plan-only   # show plan, no registration
python3 bakery/bakery.py manifest.json -o updated.json
```

## Usage (freebsd-host)

```sh
# 1. Run bakery.py on linux-host first (fills _bakery.plan into manifest)
python3 bakery/bakery.py manifest.json -o manifest.baked.json

# 2. Copy to freebsd-host and execute the plan
scp manifest.baked.json freebsd-host:/tmp/
scp bakery/bake.freebsd.sh freebsd-host:/tmp/
ssh freebsd-host "sh /tmp/bake.freebsd.sh /tmp/manifest.baked.json jailrun-native-base"
```

## ESP-IDF / IDF_TOOLS bypass

When native toolchain binaries are on PATH, skip `idf.py`'s tool manager:
```sh
export IDF_PATH=/path/to/esp-idf
export PATH=/usr/local/xtensa-esp-elf/bin:$PATH
export ESP_IDF_VERSION=5.3    # satisfies version-gating in components
# Run idf.py directly; it picks up the toolchain from PATH
idf.py build
```

Do NOT source `$IDF_PATH/export.sh` — that invokes `idf_tools.py` which will
attempt to download Espressif's Linux toolchain binaries and may error on
FreeBSD (tool not listed in `tools.json` for the host platform).

Reference: [ESP-IDF idf-tools](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/tools/idf-tools.html)

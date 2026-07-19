# Recipe: cmake + ninja

**Provider:** `pkg:cmake` + `pkg:ninja`
**Status:** ready — standard FreeBSD packages

## How to obtain natively on FreeBSD

```sh
pkg install cmake ninja
```

## Artifact paths

| Package | Binary path               |
|---------|---------------------------|
| cmake   | `/usr/local/bin/cmake`    |
| ninja   | `/usr/local/bin/ninja`    |
| gmake   | `/usr/local/bin/gmake`    |

Note: FreeBSD ships BSD `make` at `/usr/bin/make`. ESP-IDF requires GNU make;
use `gmake` (`pkg install gmake`). The bakery maps `pkg:gmake` → `/usr/local/bin/gmake`.

## ESP-IDF wiring

ESP-IDF 5.x uses CMake + Ninja by default (`idf.py build` invokes cmake then
ninja). Both are native FreeBSD binaries — no Linuxulator needed.

Set before running `idf.py`:
```sh
export IDF_PATH=/path/to/esp-idf
export PATH=/usr/local/bin:$PATH   # cmake, ninja, gmake
```

## Caveats

- **CMake version:** ESP-IDF 5.3 requires CMake >= 3.24. FreeBSD 14's
  `cmake` package tracks upstream closely; check `pkg info cmake` for the
  installed version.
- **Ninja version:** No known floor; current FreeBSD package (1.11+) is fine.
- **Parallel builds:** `-j$(nproc)` works on FreeBSD (`nproc` from `pkg install
  gnugrep` or use `sysctl -n hw.ncpu`).

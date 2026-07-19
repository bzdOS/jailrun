# Recipe: python

**Provider:** `pkg:python311` (or `pkg:python312`, `pkg:python313`)
**Status:** ready — standard FreeBSD package

## How to obtain natively on FreeBSD

```sh
pkg install python311
```

Installs to `/usr/local/bin/python3.11`.

Multiple versions coexist under versioned names; `python3` symlink at
`/usr/local/bin/python3` points to the default configured via `pythonX_enable`
in `/etc/rc.conf` or via `pkg set -n`.

## Artifact paths

| Package      | Binary path                     |
|--------------|---------------------------------|
| python311    | `/usr/local/bin/python3.11`     |
| python312    | `/usr/local/bin/python3.12`     |
| python313    | `/usr/local/bin/python3.13`     |

The bakery uses the versioned path (e.g. `python3.11`) so the runtime can
bind-mount it at an exact location inside the jail, overriding the image's
`/usr/bin/python3` regardless of symlink state.

## Caveats

- **pip / venv:** `python311` pulls in pip and venv support. The ESP-IDF
  virtual-env (`~/.espressif/python_env/`) must be re-created inside the
  jail from the native Python binary. Set `IDF_PYTHON=/usr/local/bin/python3.11`
  before running `idf.py`.
- **esphome:** esphome's `requirements.txt` installs cleanly under FreeBSD
  python; no known ABI-breaking C extensions for the core build path.
- **Version pin:** probe (S2) should record the exact python version from
  the OCI image and map it to the nearest FreeBSD package version. Drift
  between e.g. 3.11.x and 3.12.x can affect esphome component APIs.

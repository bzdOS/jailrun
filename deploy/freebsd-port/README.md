# jailrun FreeBSD port

To build and install jailrun from this port skeleton:

## Quick install (from a FreeBSD host with git)

```sh
# Clone jailrun
git clone https://github.com/bzdOS/jailrun /usr/ports/sysutils/jailrun

# Or symlink this directory
ln -s /path/to/jailrun/deploy/freebsd-port /usr/ports/sysutils/jailrun

# Copy distinfo (or run `make makesum` inside the port dir)
cd /usr/ports/sysutils/jailrun
make install
```

## Prerequisites

- FreeBSD 14.x or 15.x
- ZFS pool (default: `zroot`, override with `JAILRUN_ZPOOL`)
- `linux64` kernel module loaded: `kldload linux64`
- `kern.racct.enable=1` in `/boot/loader.conf`

## What gets installed

| Path | Purpose |
|---|---|
| `PREFIX/bin/jailrun` | CLI binary (zipapp) |
| `PREFIX/share/man/man1/jailrun.1` | Man page |
| `/var/jailrun/` | Runtime data (images, bases, runs) |
| `/var/cache/jailrun/` | OCI layout + manifest cache |
| `/var/db/jailrun/` | Run-state SQLite database |
| `/var/log/jailrun/` | Per-run log files |

## Verifying

```sh
jailrun doctor
jailrun help
jailrun run --rm alpine:latest sh -c 'echo hello'
```

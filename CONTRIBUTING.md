# Contributing to jailrun

## The shape of the work
jailrun is **one runtime, four subsystems** (`runtime/ probe/ bakery/ store/`) built
against **one contract** in [`ARCHITECTURE.md`](ARCHITECTURE.md): the Substitution
Manifest (`schemas/substitution-manifest.schema.json`) and the Store API. Change the
contract there first; the subsystems follow.

Honor the **cross-seam invariants** (ARCHITECTURE.md §"Cross-seam invariants"):
1. `status: native` ⟺ a real native artifact exists at `native.artifact_path`.
2. `linuxulator.required` is derived (OR over `status == linuxulator`), never hand-set.
3. The static syscall-gap list is advisory; the live smoke is the source of truth.

## Validating
Most of jailrun only *means* anything on **FreeBSD** (jails / ZFS / Linuxulator). On a
dev machine you can: `python3 -m py_compile` the modules, run `probe/test_classify.py`,
and validate a manifest against the schema. Everything else is proven by the
`*.freebsd.sh` scripts on a real FreeBSD 15 host — keep unproven steps marked
`# UNVERIFIED` until they run there.

## Style
- Python 3, standard library + thin shell-outs (no heavy deps in the runtime path).
- Keep secrets and host-specific data out of the tree (no IPs, no internal hostnames).

## Publishing (maintainers)
Target: **github.com/bzdOS/jailrun**, license MIT. Before a public push: scrub internal
data, set GitHub topics + homepage, and add a CHANGELOG entry. Suggested topics:
`freebsd` `jails` `containers` `oci` `docker-alternative` `zfs` `linuxulator` `esphome`
`embedded` `cli`.

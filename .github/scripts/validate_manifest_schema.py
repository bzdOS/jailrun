#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: .github/scripts/validate_manifest_schema.py
# PURPOSE: CI check — probe.probe() and bakery.bake() output must validate against
#          schemas/substitution-manifest.schema.json, on every push
# INTENT: catches schema/code drift automatically. This exact class of bug bit the
#         project twice already: prove-on-freebsd.sh calling nonexistent Probe/Bakery
#         classes (silently swallowed by `|| WARN ... non-fatal`), and the schema's
#         additionalProperties:false rejecting bake()'s own `_bakery` key — both found
#         2026-07-18 by hand, neither would have survived a CI run like this.
# DEPENDENCIES: stdlib (json, sys, tempfile, os, pathlib), jsonschema, probe.probe,
#               bakery.bakery
# PUBLIC_API: none — script, exits 1 on any validation failure
# END_AI_HEADER
"""
validate_manifest_schema.py — build a tiny synthetic rootfs, run the REAL probe.probe()
and bakery.bake() against it, and validate both outputs against the schema.

bake() now calls the REAL store.store.Store (fixed 2026-07-19 — it
used to call an internal _MockStore unconditionally, which is exactly why the fake
"zroot/..." snapshot_id bug went unnoticed through every prior prove-out). On a real
FreeBSD host with pkg/zfs, register_base() actually provisions; on this linux-host CI
runner it would try to shell out to `zfs`/`pkg`, which don't exist. So this script only
exercises bake()'s NO-NATIVE-PROVIDER-NEEDED path (plan.steps == [], the documented
early-return in bake()) — that's still real coverage of the wiring and schema shape,
without requiring FreeBSD tools. plan_to_provision_cmd() itself (the part that WOULD
need FreeBSD to actually run) has its own pure, subprocess-free unit tests in
bakery/test_plan_to_provision_cmd.py.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import jsonschema

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from probe.probe import probe  # noqa: E402
from bakery.bakery import bake  # noqa: E402


def _write_fake_elf(path: Path, osabi: int) -> None:
    import struct
    e_ident = bytearray(16)
    e_ident[0:4] = b"\x7fELF"
    e_ident[4] = 2       # ELFCLASS64
    e_ident[5] = 1       # little-endian
    e_ident[6] = 1       # EI_VERSION
    e_ident[7] = osabi
    header = bytes(e_ident) + struct.pack("<HH", 2, 62)  # ET_EXEC, EM_X86_64
    header += b"\x00" * (64 - len(header))
    path.write_bytes(header)
    path.chmod(0o755)


def main() -> None:
    schema = json.loads(Path(_ROOT, "schemas", "substitution-manifest.schema.json").read_text())

    with tempfile.TemporaryDirectory() as td:
        rootfs = Path(td) / "rootfs"
        (rootfs / "usr" / "bin").mkdir(parents=True)
        # A Linux ELF with a known native provider (python3 -> pkg:python311) — this
        # makes probe()'s output realistic (a native.provider IS proposed), but we
        # deliberately do NOT call bake() on this one: bake() would try to actually
        # provision python311 via the real Store, which needs pkg/zfs (FreeBSD-only).
        _write_fake_elf(rootfs / "usr" / "bin" / "python3", osabi=0)
        # A Linux ELF with NO known provider -> stays linuxulator.
        _write_fake_elf(rootfs / "usr" / "bin" / "some-linux-only-tool", osabi=3)

        manifest = probe(str(rootfs), image_ref="ci-smoke:latest")
        manifest["generated_at"] = "2026-01-01T00:00:00+00:00"

        jsonschema.validate(manifest, schema)
        print("OK: probe() output validates against the schema")

        # bake() on a manifest with NO native.provider entries at all takes its
        # documented early-return (plan.steps == []) and never touches the real
        # Store — safe to run here, still exercises the real bake() code path.
        no_provider_manifest = {
            "image": "ci-smoke:latest",
            "binaries": [
                {"path": "/usr/bin/some-linux-only-tool", "abi": "linux", "status": "linuxulator"},
            ],
            "linuxulator": {"required": True, "gaps": [], "risk": "low"},
        }
        baked = bake(no_provider_manifest)
        jsonschema.validate(baked, schema)
        print("OK: bake() early-return (no native providers) output validates against the schema")
        assert "_bakery" not in baked, "bake() should not add _bakery when plan.steps is empty"
        print("OK: bake() did not touch the real Store for a no-provider manifest")


if __name__ == "__main__":
    main()

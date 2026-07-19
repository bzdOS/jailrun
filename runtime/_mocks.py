# START_AI_HEADER
# MODULE: runtime/_mocks.py
# PURPOSE: stub (NotImplementedError) implementations of S2/S3/S4 seam interfaces
# INTENT: mocks — not real implementations; keeps engine.py importable for static
#         analysis and unit tests on Linux/non-FreeBSD hosts where the real store/
#         probe/bakery modules cannot be imported; raises NotImplementedError at
#         call time so a FreeBSD test harness detects missing seams early
# DEPENDENCIES: stdlib (uuid, typing)
# PUBLIC_API: MockStore, MockProbe, MockBakery
# END_AI_HEADER
"""
runtime/_mocks.py — stub implementations of S2/S3/S4 seams.

These are used when the real store/probe/bakery modules are not yet importable
(e.g. when running py_compile on Linux/linux-host, or in unit tests).

Each mock class implements exactly the Store API surface documented in
ARCHITECTURE.md Seam 2, plus the probe/bakery call shapes that engine.py uses.
They do NOT do real work — they raise NotImplementedError at call time so that
a test harness on freebsd-host can detect missing seams early.

Design rule: mocks live here, NOT inlined in engine.py, so engine.py stays
importable for static analysis and unit testing even without S2/S3/S4.
"""

from __future__ import annotations

import uuid
from typing import Any


# MockStore:start
#   purpose: stub for store.store (S3 seam) — raises NotImplementedError on every call
#   sideEffects: none (raises before any I/O)
# MockStore:end
class MockStore:
    """
    Stub for store.store — S3.

    API (from ARCHITECTURE.md Seam 2):
      resolve(image_ref)                     -> image_id
      unpack(image_id)                       -> snapshot_id
      register_base(name, provision)         -> snapshot_id
      clone(snapshot_id)                     -> (rootfs_path, handle)
      mount(handle, binds=[(host,dest,ro)])  -> None
      destroy(handle)                        -> None
    """

    # resolve: mock — raises NotImplementedError; real impl resolves image_ref -> image_id
    @staticmethod
    def resolve(image_ref: str) -> str:  # noqa: ARG004
        raise NotImplementedError(
            "store.resolve not implemented — S3 (store/) not yet built. "
            "Run on freebsd-host once store/ seam is available."
        )

    # unpack: mock — raises NotImplementedError; real impl unpacks image_id -> snapshot_id
    @staticmethod
    def unpack(image_id: str) -> str:  # noqa: ARG004
        raise NotImplementedError("store.unpack not implemented — S3 not yet built.")

    # register_base: mock — raises NotImplementedError; real impl registers base -> snapshot_id
    @staticmethod
    def register_base(name: str, provision: Any) -> str:  # noqa: ARG004
        raise NotImplementedError("store.register_base not implemented — S3 not yet built.")

    # base_mountpoint: mock — raises NotImplementedError; real impl resolves snapshot_id -> host Path
    @staticmethod
    def base_mountpoint(snapshot_id: str) -> Any:  # noqa: ARG004
        raise NotImplementedError("store.base_mountpoint not implemented — S3 not yet built.")

    # clone: mock — raises NotImplementedError; real impl clones snapshot -> (rootfs_path, handle)
    @staticmethod
    def clone(snapshot_id: str) -> tuple[str, str]:  # noqa: ARG004
        raise NotImplementedError("store.clone not implemented — S3 not yet built.")

    # mount: mock — raises NotImplementedError; real impl mounts handle with bind list
    @staticmethod
    def mount(handle: str, binds: list[tuple[str, str, bool]]) -> None:  # noqa: ARG004
        raise NotImplementedError("store.mount not implemented — S3 not yet built.")

    # destroy: mock — raises NotImplementedError; real impl runs zfs destroy + unmount
    @staticmethod
    def destroy(handle: str) -> None:  # noqa: ARG004
        raise NotImplementedError("store.destroy not implemented — S3 not yet built.")


# MockProbe:start
#   purpose: stub for probe.probe (S2 seam) — raises NotImplementedError on every call
#   sideEffects: none (raises before any I/O)
# MockProbe:end
class MockProbe:
    """
    Stub for probe.probe — S2.

    probe(rootfs_path, image_ref) -> manifest dict conforming to the
    substitution manifest schema.
    """

    # probe: mock — raises NotImplementedError; real impl inspects rootfs and returns manifest dict
    @staticmethod
    def probe(rootfs_path: str, image_ref: str) -> dict[str, Any]:  # noqa: ARG004
        raise NotImplementedError(
            "probe.probe not implemented — S2 (probe/) not yet built."
        )


# MockBakery:start
#   purpose: stub for bakery.bakery (S4 seam) — raises NotImplementedError on every call
#   sideEffects: none (raises before any I/O)
# MockBakery:end
class MockBakery:
    """
    Stub for bakery.bakery — S4.

    bake(manifest) -> manifest dict with native.artifact_path filled for
    status=native binaries.
    """

    # bake: mock — raises NotImplementedError; real impl fills native.artifact_path in manifest
    @staticmethod
    def bake(manifest: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG004
        raise NotImplementedError(
            "bakery.bake not implemented — S4 (bakery/) not yet built."
        )

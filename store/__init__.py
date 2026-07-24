# START_AI_HEADER
# MODULE: store/__init__.py
# PURPOSE: ZFS-native OCI image store package; re-exports the Store class as the package's public surface
# INTENT: Seam 2 contract point — runtime/engine.py imports Store from here; keeps the store subsystem replaceable
# DEPENDENCIES: store.store (Store class), skopeo, bsdtar, zfs, jail, mount_nullfs (validated on FreeBSD 15.1)
# PUBLIC_API: Store
# END_AI_HEADER
# jailrun store package
# S3 — ZFS-native OCI image store.
#
# Public API (Seam 2 — see ARCHITECTURE.md):
#   from store import Store
#
# The Store class shells out to skopeo / bsdtar / zfs / jail / mount_nullfs.
# All FreeBSD-specific calls are validated end-to-end (soak, adversarial tests).

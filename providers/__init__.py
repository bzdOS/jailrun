# START_AI_HEADER
# MODULE: providers/__init__.py
# PURPOSE: load the hand-maintained substitution lookup tables (provider map, pkg/port
#          artifact paths, multi-binary pkg set) from schema-validated JSON data files
#          under providers/ instead of hard-coded Python dict/set literals.
# INTENT: roadmap 0.3 — "the registry becomes data, not code". probe.py's PROVIDER_MAP
#         and bakery.py's PKG_ARTIFACTS / PORT_ARTIFACTS / MULTI_BINARY_PKGS used to be
#         hand-written dict/set literals inside those modules; this package is now the
#         single source of truth for that data (providers/*.json, validated against
#         providers/registry.schema.json), so probe.py and bakery.py just import the
#         SAME objects from here. Growing the registry becomes a data change (agent-
#         contributed JSON) rather than a code edit. The BuildRecipe/RECIPE_REGISTRY
#         objects in bakery.py carry logic (pkg_deps/port_deps/status/notes), not just
#         a plain lookup, and stay in code — only the plain dict/set tables moved.
# DEPENDENCIES: stdlib only (json, pathlib)
# PUBLIC_API: PROVIDER_MAP: dict[str, str]; PKG_ARTIFACTS: dict[str, str];
#             PORT_ARTIFACTS: dict[str, str]; MULTI_BINARY_PKGS: frozenset[str]
# END_AI_HEADER
"""
providers/__init__.py — loads providers/*.json once at import time and exposes them
as the same Python objects (same types) probe.py and bakery.py used to define inline.

Data files are located via Path(__file__).parent — NOT via the process cwd — so this
works no matter where jailrun is invoked from (repo root, installed package, CI runner).
Loaded once at module import and cached as module-level constants: no per-call file I/O
in probe's/bakery's hot paths (propose_native(), resolve_pkg(), resolve_port(), ...).
"""

import json
from pathlib import Path

_DATA_DIR = Path(__file__).parent


# _load_json:start
#   purpose: read and parse one providers/*.json data file
#   input:
#     filename: str — basename of the JSON file, e.g. "provider-map.json"
#   output:
#     data: dict — parsed top-level JSON object (schema_version + the mapping/list)
#   sideEffects: opens and reads filename from _DATA_DIR, relative to this module file
#                (read-only file I/O); raises FileNotFoundError / json.JSONDecodeError
#                on a missing or malformed data file — fail loud at import time rather
#                than silently starting with an empty registry.
def _load_json(filename: str) -> dict:
    return json.loads((_DATA_DIR / filename).read_text())
# _load_json:end


_provider_map_data = _load_json("provider-map.json")
_pkg_artifacts_data = _load_json("pkg-artifacts.json")
_port_artifacts_data = _load_json("port-artifacts.json")
_multi_binary_pkgs_data = _load_json("multi-binary-pkgs.json")

# PUBLIC_API — same shapes/types probe.py / bakery.py used to define inline.
# Key: basename (lower-case) of the Linux binary.
# Value: FreeBSD provider string:  pkg:<name>  | port:<origin> | build:<id>
PROVIDER_MAP: dict[str, str] = dict(_provider_map_data["providers"])

# pkg:<name>  ->  artifact_path_in_base (paths under /usr/local, FreeBSD PREFIX)
PKG_ARTIFACTS: dict[str, str] = dict(_pkg_artifacts_data["artifacts"])

# port:<origin>  ->  artifact_path
PORT_ARTIFACTS: dict[str, str] = dict(_port_artifacts_data["artifacts"])

# Packages that install MANY distinctly-named binaries rather than one binary
# matching the package name (see bakery.fill_artifact_paths()).
MULTI_BINARY_PKGS: frozenset[str] = frozenset(_multi_binary_pkgs_data["packages"])

__all__ = ["PROVIDER_MAP", "PKG_ARTIFACTS", "PORT_ARTIFACTS", "MULTI_BINARY_PKGS"]

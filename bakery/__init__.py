# START_AI_HEADER
# MODULE: bakery/__init__.py
# PURPOSE: native-supply package marker (S4) — makes `bakery` a real package, not
#          merely an implicit namespace package
# INTENT: pyproject.toml already declares bakery as a package
#         (packages = ["runtime", "probe", "store", "bakery"]), and engine.py's
#         `from bakery import bakery as _bakery_module` already relied on Python's
#         implicit namespace-package fallback to work at all. Added 2026-07-19
#         because pytest's test collection for
#         bakery/test_plan_to_provision_cmd.py (`from bakery.bakery import ...`)
#         failed without it ("'bakery' is not a package") — matches store/'s
#         existing __init__.py, which the same style of test already relied on.
# DEPENDENCIES: none
# PUBLIC_API: none — see bakery.bakery for the real public surface
# END_AI_HEADER

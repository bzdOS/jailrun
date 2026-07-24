#!/bin/sh
# ci-local.sh — run the same gates as .github/workflows/ci.yml, locally, so CI
# quality can be verified (and the workflow kept honest) without GitHub Actions.
# Exists because the public push of .github/workflows/ci.yml is OAuth-scope-blocked
# (workflow files need the token's `workflow` scope) — the workflow and this script
# are kept in lockstep; when CI is enabled on GitHub this exact sequence runs there.
#
# Gates (fail-fast):
#   1. py_compile every module
#   2. shell syntax check on every *.sh
#   3. full pytest suite
#   4. substitution-manifest schema validation
#   5. provider registry data validation (providers/*.json + verification.json)
#
# Usage: sh ci-local.sh        (from the repo root; run as the user, no root needed)
set -eu

cd "$(dirname "$0")"
echo "== 1/5 py_compile =="
python3 -m py_compile \
  store/store.py \
  probe/probe.py \
  bakery/bakery.py \
  runtime/engine.py \
  runtime/cli.py \
  runtime/lifecycle.py \
  runtime/_mocks.py \
  runtime/rundb.py \
  runtime/gc.py \
  runtime/explain.py \
  runtime/scan.py \
  runtime/netpolicy.py \
  runtime/netops.py \
  providers/__init__.py \
  providers/backfill.py
echo "   OK"

echo "== 2/5 shell syntax (*.sh) =="
for f in deploy/*.sh store/*.sh probe/*.sh bakery/*.sh runtime/*.sh; do
  echo "   sh -n $f"
  sh -n "$f"
done
echo "   OK"

echo "== 3/5 pytest (full suite) =="
python3 -m pytest -q

echo "== 4/5 substitution-manifest schema =="
python3 .github/scripts/validate_manifest_schema.py >/dev/null
echo "   OK"

echo "== 5/5 provider registry data =="
python3 .github/scripts/validate_provider_data.py >/dev/null
echo "   OK"

echo "== CI-local: ALL GREEN =="

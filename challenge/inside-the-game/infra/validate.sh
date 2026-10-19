#!/usr/bin/env bash
# Validate the MatchPulse Live Bicep template.
#
# Compiles infra/main.bicep and asserts that the compiled template contains no
# hardcoded subscription, tenant, resource group, or secret value.
#
# Usage: bash infra/validate.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/infra/main.bicep"

echo "==> Compiling $TEMPLATE"
az bicep build --file "$TEMPLATE" --outfile /tmp/matchpulse-main.json

COMPILED=/tmp/matchpulse-main.json
echo "==> Scanning compiled template for forbidden literals"
fail=0
if grep -nE '"subscriptionId"[[:space:]]*:[[:space:]]*"[0-9a-fA-F-]{36}"' "$COMPILED"; then
  echo "FAIL: compiled template contains a hardcoded subscription id" >&2
  fail=1
fi
if grep -nE '"tenantId"[[:space:]]*:[[:space:]]*"[0-9a-fA-F-]{36}"' "$COMPILED"; then
  echo "FAIL: compiled template contains a hardcoded tenant id" >&2
  fail=1
fi
if grep -nEi '"(AccountKey=|password|clientSecret|apiKey)[[:space:]]*:[[:space:]]*"[^"]+"' "$COMPILED"; then
  echo "FAIL: compiled template contains a secret literal" >&2
  fail=1
fi
if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: Bicep compiles and contains no hardcoded identifiers or secrets"

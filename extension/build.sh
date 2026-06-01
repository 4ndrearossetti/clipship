#!/usr/bin/env bash
# Produce two extension packages:
#   - clipship-chrome-<ver>.zip    : Chrome / Edge / Brave (MV3 service_worker)
#   - clipship-firefox-<ver>.zip   : Firefox / AMO (adds background.scripts
#                                    fallback that Mozilla's linter requires)
#
# Requires `web-ext` and `jq` (sudo apt install jq).
set -euo pipefail

cd "$(dirname "$0")"

ARTIFACTS="${1:-./web-ext-artifacts}"
mkdir -p "$ARTIFACTS"

VERSION=$(jq -r .version manifest.json)

# --- Chrome / Edge / Brave build --------------------------------------------
# The source manifest is already Chrome-clean (service_worker only, no
# "background.scripts" warning in chrome://extensions). Just package it.
echo "→ Building Chrome / Edge / Brave package…"
web-ext build \
  --source-dir=. \
  --overwrite-dest \
  --artifacts-dir="$ARTIFACTS" \
  --filename="clipship-chrome-$VERSION.zip" \
  >/dev/null

# --- Firefox / AMO build ----------------------------------------------------
# Mozilla's addons-linter requires "background.scripts" alongside
# "service_worker" even for modern Firefox. We synthesise that variant in a
# staging directory so the source tree stays Chrome-clean.
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT
cp -r ./* "$STAGING"/
jq '.background = {service_worker: "background.js", scripts: ["background.js"]}' \
  manifest.json > "$STAGING/manifest.json"

echo "→ Linting Firefox build…"
web-ext lint --source-dir="$STAGING" --warnings-as-errors=false || true

echo "→ Building Firefox / AMO package…"
web-ext build \
  --source-dir="$STAGING" \
  --overwrite-dest \
  --artifacts-dir="$ARTIFACTS" \
  --filename="clipship-firefox-$VERSION.zip" \
  >/dev/null

echo
echo "Built:"
ls -lh "$ARTIFACTS"/clipship-*-"$VERSION".zip
echo
echo "Next: upload clipship-firefox-$VERSION.zip to AMO."
echo "      (see docs/amo-submission.md)"

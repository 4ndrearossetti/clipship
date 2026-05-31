#!/usr/bin/env bash
# Build a Firefox-AMO-ready zip and Chrome-loadable zip of the extension.
# Requires `web-ext` (npm install -g web-ext).
set -euo pipefail

cd "$(dirname "$0")"

ARTIFACTS="${1:-./web-ext-artifacts}"
mkdir -p "$ARTIFACTS"

echo "→ Linting…"
web-ext lint --warnings-as-errors=false

echo
echo "→ Building…"
web-ext build --overwrite-dest --artifacts-dir="$ARTIFACTS"

VERSION=$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")
echo
echo "Built: $ARTIFACTS/clipship-$VERSION.zip"
echo
echo "Next: upload to https://addons.mozilla.org/developers/addon/submit/distribution"
echo "      (see docs/amo-submission.md for the full walkthrough)"

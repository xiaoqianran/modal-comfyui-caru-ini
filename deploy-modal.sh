#!/usr/bin/env bash
# Deploy without changing the ComfyUI web route; print its existing URL.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
WORKFLOW="${1:-${WORKFLOW:-comfyui-pixal3d-trellis2}}"
export WORKFLOW

if [[ ! -d "workflows/$WORKFLOW" ]]; then
  echo "ERROR: Unknown workflow: $WORKFLOW" >&2
  exit 1
fi
for required_file in models.json plugins.json; do
  if [[ ! -f "workflows/$WORKFLOW/$required_file" ]]; then
    echo "ERROR: Missing workflows/$WORKFLOW/$required_file" >&2
    exit 1
  fi
done
if ! find "workflows/$WORKFLOW" -type f -name '*.json' ! -name 'models.json' ! -name 'plugins.json' | grep -q .; then
  echo "ERROR: No workflow graph JSON found for $WORKFLOW" >&2
  exit 1
fi

echo "Deploying Modal ComfyUI workflow: $WORKFLOW"
uv run modal deploy comfyui.py
echo
if ! uv run python show-web-url.py --workflow "$WORKFLOW"; then
  echo "WARNING: Deployment succeeded, but URL lookup failed." >&2
fi
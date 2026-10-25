#!/usr/bin/env bash
# Linux: create missing Modal Secrets from .env; never overwrite existing ones.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

[[ -f .env ]] || { echo "ERROR: .env not found" >&2; exit 1; }
command -v uv >/dev/null || { echo "ERROR: uv not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 not found" >&2; exit 1; }

echo "Modal profile:"
uv run modal profile current
echo "Checking Modal Secrets..."
secret_list="$(uv run modal secret list)"

get_env() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys
key = sys.argv[1]
for line in Path(".env").read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()
    if line.startswith("export "):
        line = line[7:].strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, val = line.split("=", 1)
    if k.strip() == key:
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        print(val, end="")
        break
PY
}

ensure_secret() {
  local name="$1" required="$2"
  shift 2
  if printf '%s\n' "$secret_list" | grep -Eq "(^|[[:space:]│┃])${name}([[:space:]│┃]|$)"; then
    echo "EXISTS: $name"
    return
  fi
  local tmp mapping src dst val
  tmp="$(mktemp)"
  chmod 600 "$tmp"
  for mapping in "$@"; do
    src="${mapping%%:*}"
    dst="${mapping#*:}"
    val="$(get_env "$src")"
    if [[ -z "$val" ]]; then
      rm -f "$tmp"
      if [[ "$required" == true ]]; then
        echo "ERROR: $src is missing; cannot create $name" >&2
        return 1
      fi
      echo "SKIP optional: $name ($src not set)"
      return
    fi
    # Quote dotenv values safely, including spaces and special characters.
    DST="$dst" VAL="$val" python3 - "$tmp" <<'PY'
import json, os, sys
with open(sys.argv[1], "a", encoding="utf-8") as f:
    f.write(os.environ["DST"] + "=" + json.dumps(os.environ["VAL"], ensure_ascii=False) + "\n")
PY
  done
  echo "CREATE: $name"
  if uv run modal secret create "$name" --from-dotenv "$tmp"; then
    rm -f "$tmp"
  else
    rm -f "$tmp"
    return 1
  fi
}

ensure_secret huggingface-s3 true HF_S3_ACCESS_KEY_ID:AWS_ACCESS_KEY_ID HF_S3_SECRET_ACCESS_KEY:AWS_SECRET_ACCESS_KEY
ensure_secret cloudflare-r2 true AWS_ACCESS_KEY_ID:AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY:AWS_SECRET_ACCESS_KEY
ensure_secret huggingface-secret false HF_TOKEN:HF_TOKEN
ensure_secret github false GITHUB_TOKEN:GITHUB_TOKEN

echo "Secret setup completed."
uv run modal secret list
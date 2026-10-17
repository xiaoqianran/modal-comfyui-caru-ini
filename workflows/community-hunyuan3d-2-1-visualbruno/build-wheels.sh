#!/usr/bin/env bash
# Build Hunyuan3D wheels in a Linux environment matching the Modal image.
set -euo pipefail

[[ $# == 0 ]] || { echo "Usage: bash workflows/community-hunyuan3d-2-1-visualbruno/build-wheels.sh" >&2; exit 2; }
[[ "$(uname -s)" == "Linux" ]] || { echo "Linux is required" >&2; exit 2; }
command -v nvcc >/dev/null || { echo "CUDA toolkit (nvcc) is required" >&2; exit 2; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Read the pinned source version from this workflow's plugins.json (no jq needed).
config="$(python - "$root/plugins.json" <<'PY'
import json
import re
import sys
from pathlib import Path

plugins = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["comfy_plugins_ext"]
if len(plugins) != 1:
    sys.exit("Expected exactly one external plugin in plugins.json")
url, commit = plugins[0]["url"], plugins[0].get("commit", "")
if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", url):
    sys.exit("Invalid GitHub source URL")
if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
    sys.exit("Plugin commit must be a full 40-character SHA")
print(url)
print(commit)
PY
)"
mapfile -t source <<< "$config"
repo="${source[0]}"
commit="${source[1]}"

export CC="${CC:-gcc}" CXX="${CXX:-g++}"
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-8.9}"
output="$root/wheels"
mkdir -p "$output"
if compgen -G "$output/*.whl" > /dev/null; then
  echo "wheels/ already contains wheel files; move them before a new build" >&2
  exit 2
fi

python -c "import torch; from torch.utils.cpp_extension import CUDA_HOME; assert CUDA_HOME, 'CUDA_HOME not found'"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git clone --recurse-submodules "$repo" "$tmp/node"
git -C "$tmp/node" checkout --detach "$commit"
git -C "$tmp/node" submodule update --init --recursive

for module in custom_rasterizer DifferentiableRenderer; do
  (cd "$tmp/node/hy3dpaint/$module" && python -m pip wheel . --no-deps --no-build-isolation -w "$output")
done

(
  cd "$output"
  sha256sum ./*.whl > SHA256SUMS
  {
    echo "source=$repo"
    echo "commit=$commit"
    echo "cuda_arch=$TORCH_CUDA_ARCH_LIST"
    echo "compiler=$(gcc -dumpfullversion)"
    echo "nvcc=$(nvcc --version | tail -n 1)"
    python -c "import platform, torch; print('python=' + platform.python_version()); print('torch=' + torch.__version__); print('torch_cuda=' + str(torch.version.cuda)); print('cxx11_abi=' + str(torch._C._GLIBCXX_USE_CXX11_ABI))"
  } > BUILD-INFO.txt
)
echo "Wheel artifacts ready in $output (not uploaded)."

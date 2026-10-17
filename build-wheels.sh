#!/usr/bin/env bash
# Run inside a Linux environment matching the target Modal ComfyUI image.
set -euo pipefail

if [[ $# != 1 || ! "$1" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "Usage: bash build-wheels.sh <full-40-character-plugin-commit>" >&2
  exit 2
fi
[[ "$(uname -s)" == "Linux" ]] || { echo "Linux is required" >&2; exit 2; }
command -v nvcc >/dev/null || { echo "CUDA toolkit (nvcc) is required" >&2; exit 2; }

export CC="${CC:-gcc}" CXX="${CXX:-g++}"
export TORCH_CUDA_ARCH_LIST="${TORCH_CUDA_ARCH_LIST:-8.9}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
output="$root/wheels"
mkdir -p "$output"
if compgen -G "$output/*.whl" > /dev/null; then
  echo "wheels/ already contains wheel files; move them before a new build" >&2
  exit 2
fi

# Fail early if the active Python does not have the same Torch as Modal.
python -c "import torch; from torch.utils.cpp_extension import CUDA_HOME; assert CUDA_HOME, 'CUDA_HOME not found'"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git clone --recurse-submodules https://github.com/visualbruno/ComfyUI-Hunyuan3d-2-1.git "$tmp/node"
git -C "$tmp/node" checkout --detach "$1"
git -C "$tmp/node" submodule update --init --recursive

for module in custom_rasterizer DifferentiableRenderer; do
  (cd "$tmp/node/hy3dpaint/$module" && python -m pip wheel . --no-deps --no-build-isolation -w "$output")
done

(
  cd "$output"
  sha256sum ./*.whl > SHA256SUMS
  {
    echo "source=https://github.com/visualbruno/ComfyUI-Hunyuan3d-2-1.git"
    echo "commit=$1"
    echo "cuda_arch=$TORCH_CUDA_ARCH_LIST"
    echo "compiler=$(gcc -dumpfullversion)"
    echo "nvcc=$(nvcc --version | tail -n 1)"
    python -c "import platform, torch; print('python=' + platform.python_version()); print('torch=' + torch.__version__); print('torch_cuda=' + str(torch.version.cuda)); print('cxx11_abi=' + str(torch._C._GLIBCXX_USE_CXX11_ABI))"
  } > BUILD-INFO.txt
)
echo "Wheel artifacts ready in $output (not uploaded)."

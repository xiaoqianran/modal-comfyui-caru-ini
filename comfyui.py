from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

import modal

from runtime_policy import resolve_policy, log_event, wait_for_server, stop_process
from storage.delivery import DEFAULT_VOLUME, token, build_receipt, publish_receipt, receipt_key, validate_receipt

# Modal 镜像构建与应用声明顺序（必须保持）：
# 1. 安装 ComfyUI。
# 2. 模型通过独立 Modal Function 下载到持久化 Volume，不参与镜像构建。
# 3. 安装工作流插件。
# 4. 编译所需的 CUDA 扩展。
# 5. 安装其余依赖并完成所有镜像构建步骤。
# 6. 最后通过 copy=False 挂载所有工作流 JSON（排除 models.json / plugins.json）。
# 7. 声明 Modal App。
# 注意：copy=False 的 add_local_file 必须位于所有 run_commands / pip_install 等构建操作之后，
# 否则 Modal 会拒绝构建；工作流 JSON 改动不应触发 CUDA 扩展重编译。

# ============================================================================
# 1. 读取 Workflow 配置：选择目录，解析模型、插件及 GPU 参数
# ============================================================================
root_dir = Path(__file__).parent
WORKFLOW = os.getenv("WORKFLOW", "comfyui-pixal3d-trellis2")
WORKFLOW_DIR = root_dir / "workflows" / WORKFLOW
REMOTE_WORKFLOW_DIR = Path("/root/workflows") / WORKFLOW
config_dir = WORKFLOW_DIR if (WORKFLOW_DIR / "models.json").exists() else REMOTE_WORKFLOW_DIR
if not (config_dir / "models.json").exists():
    raise ValueError(f"Unknown workflow: {WORKFLOW}")

model_config = json.loads((config_dir / "models.json").read_text(encoding="utf-8"))
plugin_config_path = config_dir / "plugins.json"
plugin_config = json.loads(plugin_config_path.read_text(encoding="utf-8")) if plugin_config_path.exists() else {}
models = model_config["models"]
models_ext = model_config.get("models_ext", [])
comfy_plugins = plugin_config.get("comfy_plugins", [])
comfy_plugins_ext = plugin_config.get("comfy_plugins_ext", [])
# Workflow files are intentionally not validated at module import time:
# Modal may import the module in a remote container where workflow mounts differ.


def _deploy_model_preflight():
    """Fail a direct Modal CLI deploy before GPU allocation if weights are missing."""
    # Modal imports the module on remote workers too; only the local CLI deploy
    # may perform this networked, CPU-only preparation.
    args = sys.argv[1:]
    if not modal.is_local() or not args or args[0] != "deploy":
        return
    if not any(Path(arg).name == "comfyui.py" for arg in args[1:]):
        return
    env = {**os.environ, "WORKFLOW": WORKFLOW}
    for flag in ("--ensure", "--check-only"):
        print(f"MODEL PREFLIGHT: {WORKFLOW} {flag}", flush=True)
        subprocess.run(
            [sys.executable, "-m", "modal", "run", "download-models.py", flag],
            cwd=root_dir, env=env, check=True,
        )


_deploy_model_preflight()

COMFY_MODELS_ROOT = Path("/root/comfy/ComfyUI/models")

GPU_TYPE = os.getenv("MODAL_GPU") or model_config.get("gpu", "L40S")
MODAL_REGIONS = [region.strip() for region in os.getenv("MODAL_REGION", "").split(",") if region.strip()] or None
# 根据最终 GPU 配置（包含 MODAL_GPU 覆盖）自动决定高显存模式。
IS_BLACKWELL = GPU_TYPE.strip().upper() in {"B200", "B200+", "B300"}
HIGH_VRAM_GPUS = {"H200", "B200", "B200+", "B300", "RTX-PRO-6000", "RTX PRO 6000", "RTX PRO 6000 BLACKWELL"}
HIGH_VRAM = (GPU_TYPE.strip().upper() in HIGH_VRAM_GPUS or model_config.get("highvram", False)) and not model_config.get("disable_highvram", False)
DUAL_COMFYUI = GPU_TYPE.strip().upper() == "B300"
COMFY_PORTS = (8000, 8001) if DUAL_COMFYUI else (8000,)
COMFY_VER = os.getenv("COMFY_VER")
POLICY = resolve_policy(WORKFLOW, model_config)
DELIVERY_MODE = os.getenv("COMFYUI_DELIVERY_MODE", "legacy")
if DELIVERY_MODE not in ("legacy", "cpu"):
    raise ValueError("COMFYUI_DELIVERY_MODE must be legacy or cpu")
APP_NAME = token(os.getenv("COMFYUI_APP_NAME", f"modal-{WORKFLOW}"), "app")
OUTPUT_VOLUME_NAME = token(os.getenv("COMFYUI_OUTPUT_VOLUME_NAME", DEFAULT_VOLUME), "volume")


def _deployment_version():
    # Remote imports use the exact build identity baked into the image.
    if not modal.is_local():
        return os.environ["COMFYUI_DEPLOYMENT_VERSION"]
    digest = hashlib.sha256()
    config = {"workflow": WORKFLOW, "app": APP_NAME, "volume": OUTPUT_VOLUME_NAME,
              "model_volume": os.getenv("MODEL_VOLUME_NAME", "model-store"),
              "gpu": GPU_TYPE, "regions": MODAL_REGIONS, "comfy_ver": COMFY_VER,
              "policy": vars(POLICY), "delivery": DELIVERY_MODE}
    digest.update(json.dumps(config, sort_keys=True).encode("utf-8"))
    paths = [root_dir / name for name in ("comfyui.py", "runtime_policy.py",
             "storage/delivery.py", "storage/output_formats.py", "storage/hf_sync.py",
             "requirements_comfy.txt", "snapshot_warmup.py", "delivery_probe.py",
             "wheel_build.py", "testing/qwen21_patch_memfd.py")]
    paths.extend(WORKFLOW_DIR.rglob("*.json"))
    paths.extend((root_dir / "vendor_nodes").rglob("*.py"))
    if POLICY.warmup == "h3-int8":
        paths.extend([root_dir / "testing/snapshot_h3_int8_warmup_api.json", root_dir / "inputs/001.png"])
    if WORKFLOW in {"010-minimax-h3-b200plus-suite", "010-minimax-h3-bf16-b200plus-suite", "001-minimax-h3-fl2va-bf16"}:
        paths.extend(root_dir / "inputs" / name for name in ("ref_popart_woman.png", "lastP_spin_jump_hole.png"))
    for path in sorted(paths, key=lambda x: x.relative_to(root_dir).as_posix()):
        content = path.read_bytes()
        if path.suffix in {".py", ".json", ".txt"}:
            content = content.replace(b"\r\n", b"\n")
        digest.update(path.relative_to(root_dir).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


DEPLOYMENT_VERSION = _deployment_version()
# Preserve existing public routes, but isolate explicitly named test Apps.
WEB_LABEL_PREFIX = (f"{APP_NAME[:36]}-{hashlib.sha256(APP_NAME.encode()).hexdigest()[:8]}"
                    if APP_NAME != f"modal-{WORKFLOW}" else "comfyui")

# ============================================================================
# 2. 运行时模型链接：下载统一由 download-models.py 并行分片完成；GPU 端仅读缓存。
# ============================================================================
def resolve_model_dir(model_dir: str) -> Path:
    """Resolve model_dir: absolute paths are used as-is, relative paths are
    placed under /root/comfy/ComfyUI/models/ (e.g. "checkpoints")."""
    p = Path(model_dir)
    return p if p.is_absolute() else COMFY_MODELS_ROOT / p


def link_cached_models():
    """Attach persisted model weights to ComfyUI without network or downloads."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    # Use exact revisions written by the verified downloader.
    revision_file = Path(MODEL_CACHE_DIR) / ".workflow-revisions" / f"{WORKFLOW}.json"
    revisions = json.loads(revision_file.read_text()) if revision_file.is_file() else {}
    receipt_path = Path(MODEL_CACHE_DIR) / ".workflow-hf-sizes" / f"{WORKFLOW}.json"
    sizes = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
    if sizes is None and models:
        log_event("legacy_model_cache", workflow=WORKFLOW, note="run downloader --ensure to migrate size receipts")

    for model in models:
        key = f"{model['repo_id']}/{model['filename']}"
        revision = revisions.get(key)
        try:
            cached = hf_hub_download(
                repo_id=model["repo_id"], filename=model["filename"],
                cache_dir=MODEL_CACHE_DIR, revision=revision, local_files_only=True)
        except LocalEntryNotFoundError as exc:
            # Compatibility for older caches without an exact revision manifest.
            repo_cache = Path(MODEL_CACHE_DIR) / ("models--" + model["repo_id"].replace("/", "--"))
            candidates = list((repo_cache / "snapshots").glob("*/" + model["filename"]))
            candidates = [path for path in candidates if path.is_file()]
            if revision is None and len(candidates) == 1:
                cached = str(candidates[0])
            else:
                raise RuntimeError(
                    f"Model not cached: {key} (revision={revision!r}). "
                    "Run: uv run modal run comfyui.py::download_models") from exc
        if sizes is not None:
            expected = sizes.get(key, {})
            if (expected.get("revision") != revision
                    or not isinstance(expected.get("size"), int) or expected["size"] <= 0
                    or Path(cached).stat().st_size != expected["size"]):
                raise RuntimeError(f"Model cache size/revision mismatch: {key}. Run downloader --ensure.")
        name = model.get("local_filename") or Path(model["filename"]).name
        target = resolve_model_dir(model["model_dir"]) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(cached)
        print(f"CACHE HIT {key} revision={revision or 'legacy'}")
    for model in models_ext:
        cached = Path(MODEL_CACHE_DIR) / model["filename"]
        if not cached.is_file() or cached.stat().st_size == 0:
            raise RuntimeError(f"Model missing: {cached}. Run: uv run modal run comfyui.py::download_models")
        target = resolve_model_dir(model["model_dir"]) / model["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(cached)
        print(f"CACHE HIT {model['filename']}")


# ============================================================================
# 3. 构建 Modal 基础镜像：选择 CUDA / Python，安装系统依赖与 ComfyUI
#    模型与插件暂不在此层加入，便于复用基础镜像缓存。
# ============================================================================
# Shared Modal Volume V2; ComfyUI HF snapshots occupy /comfyui/hf in the Volume, mounted at /data/hf.
MODEL_VOLUME_NAME = os.getenv("MODEL_VOLUME_NAME", "model-store")
MODEL_CACHE_DIR = "/data/hf"
vol = modal.Volume.from_name(MODEL_VOLUME_NAME, create_if_missing=True, version=2)

# construct images and install deps/custom nodes
VERSION = f"--version {COMFY_VER}" if COMFY_VER else ""
# B200 and B300 share the CUDA 13.4 / PyTorch cu134 image; other GPUs retain CUDA 13.0.
CUDA_VERSION = "13.4.3" if IS_BLACKWELL else "13.0.3"
TORCH_CUDA_INDEX = "cu134" if IS_BLACKWELL else "cu130"
TORCH_VERSION = "2.15.0" if IS_BLACKWELL else "2.14.1"
TORCHVISION_VERSION = "0.29.2" if IS_BLACKWELL else "0.29.1"
image = (
    modal.Image.from_registry(f"nvidia/cuda:{CUDA_VERSION}-devel-ubuntu22.04", add_python="3.11")
    .entrypoint([])
    .apt_install("git", "git-lfs", "libgl1-mesa-dev", "libglib2.0-0", "axel", "build-essential", "ninja-build")
    .uv_pip_install(requirements=[str(root_dir / "requirements_comfy.txt")])
    .run_commands(
        "uv pip install --system "
        f"torch=={TORCH_VERSION} torchvision=={TORCHVISION_VERSION} "
        f"--index-url https://download.pytorch.org/whl/{TORCH_CUDA_INDEX}"
    )
    .run_commands(f"comfy --skip-prompt install --nvidia --skip-torch-or-directml {VERSION}")
    .run_commands("git lfs install")
)
# ============================================================================
# 4. 独立下载镜像：不依赖插件镜像，不参与插件构建缓存
# ============================================================================
# ============================================================================
# 5. 插件由 plugins.json 声明；工作流在全部构建步骤完成后挂载
# ============================================================================
def find_cnr_ids(data) -> set[str]:
    """Find Comfy Registry IDs, including nodes inside nested subgraphs."""
    if isinstance(data, dict):
        ids = {data["cnr_id"]} if isinstance(data.get("cnr_id"), str) else set()
        children = data.values()
    elif isinstance(data, list):
        ids, children = set(), data
    else:
        return set()
    for child in children:
        ids.update(find_cnr_ids(child))
    return ids


# Only the selected workflow's JSON files determine automatically installed nodes.
node_ids = set(comfy_plugins)
if WORKFLOW_DIR.is_dir():
    for workflow_json in sorted(WORKFLOW_DIR.rglob("*.json")):
        if workflow_json.name.lower() in {"models.json", "plugins.json"}:
            continue
        node_ids.update(find_cnr_ids(json.loads(workflow_json.read_text(encoding="utf-8"))))
node_ids.discard("comfy-core")
if node_ids:
    image = image.run_commands(
        "comfy node install " + " ".join(shlex.quote(node_id) for node_id in sorted(node_ids))
    )


def install_wheel(image: modal.Image, wheel: dict) -> modal.Image:
    """Install a verified wheel from this fork's public GitHub Releases."""
    url, sha = wheel["url"], wheel["sha256"].lower()
    parts = urlsplit(url)
    filename = parts.path.rsplit("/", 1)[-1]
    if (
        parts.scheme != "https"
        or parts.netloc != "github.com"
        or parts.path.split("/")[1:5] !=
        ["xiaoqianran", "modal-comfyui-caru-ini", "releases", "download"]
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.whl", filename)
        or not re.fullmatch(r"[0-9a-f]{64}", sha)
    ):
        raise ValueError(f"Invalid wheel URL or SHA256: {url}")
    target = f"/tmp/{filename}"
    download = (
        "import hashlib, pathlib, urllib.request\n"
        f"p = pathlib.Path({target!r})\n"
        f"urllib.request.urlretrieve({url!r}, p)\n"
        f"if hashlib.sha256(p.read_bytes()).hexdigest() != {sha!r}:\n"
        "    raise RuntimeError('Wheel SHA256 mismatch')"
    )
    return image.run_commands(
        f"python -c {shlex.quote(download)} && "
        f"uv pip install --reinstall --no-deps --python $(command -v python) {shlex.quote(target)}"
    )


def install_ext_plugin(image: modal.Image, plugin: dict) -> modal.Image:
    """Install one external custom node from git into ComfyUI's custom_nodes.

    Supports optional ``branch``, ``requirements`` (a list of requirement
    files), an ``install`` script (.py), and ``ext_deps`` (a list of extra pip
    packages). User-supplied values are shell-quoted before use.
    """
    nodes_dir = "/root/comfy/ComfyUI/custom_nodes"
    url = plugin["url"]
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    work_dir = f"{nodes_dir}/{shlex.quote(name)}"

    branch = plugin.get("branch", "").strip()
    branch_opt = f"--branch {shlex.quote(branch)} " if branch else ""
    image = image.run_commands(
        f"if [ -d {work_dir}/.git ]; then "
        f"echo 'Custom node already installed: {name}'; "
        f"elif [ -e {work_dir} ]; then "
        f"echo 'Custom node directory exists without .git: {name}' >&2; exit 1; "
        f"else cd {nodes_dir} && git clone --recurse-submodules --single-branch "
        f"{branch_opt}{shlex.quote(url)}; fi"
    )

    commit = plugin.get("commit")
    if commit:
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise ValueError("Plugin commit must be a full 40-character Git SHA")
        image = image.run_commands(
            f"git -C {work_dir} checkout --detach {commit} && "
            f"git -C {work_dir} submodule update --init --recursive"
        )

    requirements = plugin.get("requirements") or []
    if requirements:
        files = " ".join(f"-r {shlex.quote(f)}" for f in requirements)
        # Resolve transitive dependencies unless explicitly disabled. Preserve
        # installed CUDA PyTorch packages with constraints to avoid replacing
        # torch/torchvision/torchaudio with incompatible wheels.
        dep_flags = ""
        if plugin.get("resolve_dependencies"):
            constraints = "/tmp/comfy-torch-constraints.txt"
            image = image.run_commands(
                "python -c "
                + shlex.quote(
                    "from importlib.metadata import distributions; "
                    "from pathlib import Path; "
                    "packages=('torch','torchvision','torchaudio'); "
                    "installed={d.metadata['Name'].lower():d.version for d in distributions()}; "
                    f"Path('{constraints}').write_text(''.join(p+'=='+installed[p]+chr(10) for p in packages if p in installed))"
                )
            )
            dep_flags = f"--constraint {constraints}"
        else:
            dep_flags = "--no-deps"
        image = image.run_commands(
            f"cd {work_dir} && uv pip install {dep_flags} "
            f"--python $(command -v python) --compile-bytecode {files}"
        )

    # Install explicit dependencies before running plugin install.py.
    ext_deps = plugin.get("ext_deps") or []
    if ext_deps:
        image = image.uv_pip_install(ext_deps, extra_options="--no-deps")

    install = plugin.get("install", "").strip()
    if install:
        if install.endswith(".py"):
            args = plugin.get("install_args", [])
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise ValueError("install_args must be a list of strings")
            command = " ".join(shlex.quote(arg) for arg in [install, *args])
            image = image.run_commands(f"cd {work_dir} && python {command}")
        else:
            print(f"Unsupported installation script: {install}")

    wheels = plugin.get("wheels") or []
    if wheels:
        expected_torch = plugin.get("wheel_torch")
        if not isinstance(expected_torch, str) or not expected_torch:
            raise ValueError("wheel_torch must be set when using prebuilt wheels")
        check = (
            "import sys, torch; "
            f"torch.__version__ == {expected_torch!r} or "
            f"sys.exit('Wheel requires PyTorch {expected_torch}')"
        )
        image = image.run_commands(f"python -c {shlex.quote(check)}")
        for wheel in wheels:
            image = install_wheel(image, wheel)
    else:
        for command in plugin.get("build_commands", []):
            image = image.run_commands(f"cd {work_dir} && {command}")

    return image


if comfy_plugins_ext:
    for plugin in comfy_plugins_ext:
        image = install_ext_plugin(image, plugin)

# Bake in the reverse-proxy fix so workflow save works behind Modal's edge
# proxy, independent of user plugin config (see vendor_nodes/reverse_proxy_fix).
image = image.add_local_dir(
    root_dir / "vendor_nodes" / "reverse_proxy_fix",
    "/root/comfy/ComfyUI/custom_nodes/reverse_proxy_fix",
    copy=True,
)

# Import signed R2 attachments directly in ComfyUI without routing via PC2.
image = image.add_local_dir(
    root_dir / "vendor_nodes" / "r2_input",
    "/root/comfy/ComfyUI/custom_nodes/r2_input",
    copy=True,
)



if WORKFLOW == "comfyui-yue2-3b":
    image = image.add_local_dir(
        root_dir / "vendor_nodes" / "yue2_instrumental",
        "/root/comfy/ComfyUI/custom_nodes/yue2_instrumental",
        copy=True,
    )

# Only mount a verified, workflow-specific warmup graph when requested.
if POLICY.warmup == "h3-int8":
    image = image.add_local_file(
        root_dir / "snapshot_warmup.py", "/root/snapshot_warmup.py", copy=True,
    ).add_local_file(
        root_dir / "testing" / "snapshot_h3_int8_warmup_api.json",
        "/root/snapshot_h3_int8_warmup_api.json",
        copy=True,
    ).add_local_file(
        root_dir / "inputs" / "001.png",
        "/root/comfy/ComfyUI/input/001.png",
        copy=True,
    )

# Official Pika FL2VA demo workflows reference these two images by filename.
# Bake them into each relevant image so a fresh ComfyUI container can run the
# workflow without a manual browser upload.
if WORKFLOW in {
    "010-minimax-h3-b200plus-suite",
    "010-minimax-h3-bf16-b200plus-suite",
    "001-minimax-h3-fl2va-bf16",
}:
    for name in ("ref_popart_woman.png", "lastP_spin_jump_hole.png"):
        image = image.add_local_file(
            root_dir / "inputs" / name,
            "/root/comfy/ComfyUI/input/" + name,
            copy=True,
        )
# ============================================================================
# Qwen-Image-2.1: fix missing os.memfd_create in ComfyUI only, without PYTHONPATH overrides.
if WORKFLOW in {"comfyui-qwen-image-2-1", "comfyui-b300-image-video-suite"}:
    image = image.add_local_file(
        root_dir / "testing" / "qwen21_patch_memfd.py",
        "/root/qwen21_patch_memfd.py",
        copy=True,
    ).run_commands("python /root/qwen21_patch_memfd.py")

# 6. Output staging Volume; R2 only stores small previews
#    wait_for_port 用于后续 ComfyUI 启动时检查 HTTP 服务是否就绪。
# ============================================================================
# Generated files stay in a persistent staging Volume until HF archive succeeds.
output_volume = modal.Volume.from_name(OUTPUT_VOLUME_NAME, create_if_missing=True, version=2)
preview_mount = modal.CloudBucketMount(
    bucket_name="modal-studio-assets",
    bucket_endpoint_url=os.getenv(
        "R2_ENDPOINT", "https://f863e42ff2096d089d4d4129197abd17.r2.cloudflarestorage.com"
    ),
    key_prefix=f"previews/{WORKFLOW}/",
    secret=modal.Secret.from_name(
        "cloudflare-r2", required_keys=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]
    ),
)
image = image.uv_pip_install("boto3", "imageio-ffmpeg").add_local_file(
    root_dir / "storage" / "hf_sync.py", "/root/hf_sync.py", copy=True
).add_local_file(
    root_dir / "storage" / "output_formats.py", "/root/storage/output_formats.py", copy=True
).env({"WORKFLOW": WORKFLOW, "MODEL_VOLUME_NAME": MODEL_VOLUME_NAME,
       "MODAL_GPU": GPU_TYPE, "MODAL_REGION": ",".join(MODAL_REGIONS or []),
       "COMFYUI_RUNTIME_PROFILE": POLICY.profile,
       "COMFYUI_SNAPSHOT_MODE": POLICY.snapshot,
       "COMFYUI_SCALEDOWN_SECONDS": str(POLICY.scaledown_window),
       "COMFYUI_WARMUP_MODE": POLICY.warmup,
       "COMFYUI_APP_NAME": APP_NAME, "COMFYUI_OUTPUT_VOLUME_NAME": OUTPUT_VOLUME_NAME,
       "COMFYUI_DELIVERY_MODE": DELIVERY_MODE,
       "COMFYUI_DEPLOYMENT_VERSION": DEPLOYMENT_VERSION})
# runtime_policy is imported when Modal deserializes this source module.
image = image.add_local_python_source("runtime_policy", "storage.delivery", "storage.output_formats")

# Runtime configuration is mounted after builds; editing models.json cannot
# invalidate the plugin installation image layers.
image = image.add_local_file(
    WORKFLOW_DIR / "models.json", f"/root/workflows/{WORKFLOW}/models.json", copy=False
)

# Mount workflow JSONs last: copy=False mounts cannot precede image build steps.
# Keep the relative directory structure and exclude only configuration JSONs.
for workflow_json in sorted(WORKFLOW_DIR.rglob("*.json")):
    if workflow_json.name.lower() in {"models.json", "plugins.json"}:
        continue
    relative = workflow_json.relative_to(WORKFLOW_DIR).as_posix()
    image = image.add_local_file(
        workflow_json,
        f"/root/comfy/ComfyUI/user/default/workflows/{WORKFLOW}/{relative}",
        copy=False,
    )
# ============================================================================
# 7. 声明 Modal App：为每个 Workflow 分配独立应用、GPU、Volume 和 Secret
# ============================================================================
app = modal.App(name=APP_NAME, image=image)

# CPU probe is registered by the local deploy process. Importing the GPU
# class remotely must not require the CPU probe's separate source mount.
if DELIVERY_MODE == "cpu" and modal.is_local():
    from delivery_probe import deployment_info
    probe_image = (modal.Image.debian_slim(python_version="3.11")
        .env({"COMFYUI_DEPLOYMENT_VERSION": DEPLOYMENT_VERSION, "COMFYUI_APP_NAME": APP_NAME,
              "WORKFLOW": WORKFLOW, "COMFYUI_OUTPUT_VOLUME_NAME": OUTPUT_VOLUME_NAME,
              "COMFYUI_INSTANCE_COUNT": str(len(COMFY_PORTS))})
        .add_local_python_source("delivery_probe"))
    app.function(image=probe_image, cpu=0.25, memory=256, timeout=15,
                 min_containers=0, max_containers=1, scaledown_window=5,
                 name="delivery_info_" + DEPLOYMENT_VERSION[:32])(deployment_info)



@app.local_entrypoint()
def download_models():
    """Use the canonical 16GB-sharded downloader; do not launch a GPU."""
    # The standalone downloader owns shard packing, retry, verification and
    # Volume V2 merge. This keeps both CLI entrypoints on exactly one path.
    subprocess.run(
        [sys.executable, "-m", "modal", "run", "download-models.py"],
        cwd=root_dir,
        env=os.environ.copy(),
        check=True,
    )


@app.cls(
    serialized=False,
    max_containers=1,  # Hard cap: never start a second B300 container for this app.
    startup_timeout=1800 if WORKFLOW == "010-minimax-h3-b200plus-suite" else 300,
    gpu=GPU_TYPE,
    region=MODAL_REGIONS,
    volumes={
        "/data": vol.with_mount_options(sub_path="/comfyui"),
        "/outputs": output_volume,
        **({"/r2-previews": preview_mount} if DELIVERY_MODE == "legacy" else {}),
    },
    secrets=[modal.Secret.from_name("huggingface-s3")] if DELIVERY_MODE == "legacy" else [],
    min_containers=0,
    scaledown_window=POLICY.scaledown_window,
    enable_memory_snapshot=POLICY.gpu_snapshot,
    experimental_options={"enable_gpu_snapshot": True} if POLICY.gpu_snapshot else {},
)
# ============================================================================
# 8. ComfyUI 生命周期：启动 Web 服务、恢复快照、执行退出清理
# ============================================================================
@modal.concurrent(max_inputs=32)
class ComfyUI:
    def _start_engine(self):
        started = time.monotonic()
        log_event("engine_start", workflow=WORKFLOW, gpu=GPU_TYPE,
                  profile=POLICY.profile, snapshot=POLICY.snapshot,
                  scaledown_window=POLICY.scaledown_window, warmup=POLICY.warmup)
        # Reload the shared model Volume before resolving newly committed weights.
        for attempt in range(3):
            try:
                vol.reload()
                link_cached_models()
                break
            except RuntimeError as exc:
                if "Model not cached:" not in str(exc) or attempt == 2:
                    raise
                print(f"Model Volume not ready ({attempt + 1}/3): {exc}", flush=True)
                time.sleep(5)
        log_event("model_cache_linked", seconds=time.monotonic()-started)
        self.procs = []
        launch_started = time.monotonic()
        for index, port in enumerate(COMFY_PORTS, start=1):
            output_dir = f"/outputs/{WORKFLOW}/instance-{index}" if DUAL_COMFYUI else f"/outputs/{WORKFLOW}"
            user_dir = f"/tmp/comfyui-user-{index}"
            temp_dir = f"/tmp/comfyui-temp-{index}"
            import shutil
            source_user_dir = Path("/root/comfy/ComfyUI/user")
            if source_user_dir.is_dir():
                shutil.copytree(source_user_dir, user_dir, dirs_exist_ok=True)
            else:
                Path(user_dir).mkdir(parents=True, exist_ok=True)
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            args = ["comfy", "launch", "--", "--listen", "0.0.0.0",
                    "--port", str(port), "--output-directory", output_dir,
                    "--temp-directory", temp_dir, "--user-directory", user_dir]
            if HIGH_VRAM:
                args.append("--highvram")
            self.procs.append(subprocess.Popen(args, start_new_session=True))
        for port, proc in zip(COMFY_PORTS, self.procs):
            wait_for_server(port, proc, timeout=300)
        log_event("server_http_ready", seconds=time.monotonic()-launch_started,
                  ports=COMFY_PORTS)
        if POLICY.warmup == "h3-int8":
            warmup_started = time.monotonic()
            # Official Modal pattern: localhost warmup inside snap=True, before
            # the container is exposed to real requests. A full H3 inference
            # (five frames, one step) initializes weights and CUDA kernels.
            import runpy
            warmup_h3 = runpy.run_path("/root/snapshot_warmup.py")["warmup_h3"]
            # Preserve the established warmup scope; a second instance
            # requires separate validation rather than doubling GPU work.
            output_root = f"/outputs/{WORKFLOW}/instance-1" if DUAL_COMFYUI else f"/outputs/{WORKFLOW}"
            warmup_h3(COMFY_PORTS[0], "/root/snapshot_h3_int8_warmup_api.json", output_root)
            log_event("warmup_completed", seconds=time.monotonic()-warmup_started)
        self.engine_initialization_seconds = time.monotonic()-started
        log_event("engine_initialized", seconds=self.engine_initialization_seconds)

    if POLICY.gpu_snapshot:
        @modal.enter(snap=True)
        def start_checkpoint(self):
            self._start_engine()
            self.snapshot_capture_id = uuid4().hex
            log_event("snapshot_capture_ready", capture_id=self.snapshot_capture_id,
                      engine_seconds=self.engine_initialization_seconds)

    @staticmethod
    def _output_snapshot():
        """Track completed-looking output files without touching temp files."""
        from storage.output_formats import EXTENSIONS, is_output_file
        root = Path("/outputs") / WORKFLOW
        snapshot = {}
        if not root.is_dir():
            return snapshot
        for path in root.rglob("*"):
            if not is_output_file(path, root, EXTENSIONS) or not path.is_file():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if stat.st_size:
                snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _commit_outputs_loop(self):
        """Periodically persist stable outputs; retry failures next cycle."""
        previous = {}
        committed = {}
        while not self.commit_stop.wait(30):
            try:
                current = self._output_snapshot()
                stable = {p: version for p, version in current.items()
                          if previous.get(p) == version}
                if stable != committed and stable:
                    started = time.monotonic()
                    with self.commit_lock:
                        output_volume.commit()
                    committed = stable
                    log_event("output_commit", files=len(stable), seconds=time.monotonic()-started)
                previous = current
            except Exception as exc:
                print(f"Output Volume V2 commit failed (will retry): {exc}", flush=True)

    @modal.enter(snap=False)
    def start_restore(self):
        started = time.monotonic()
        log_event("post_snapshot_enter", capture_id=getattr(self, "snapshot_capture_id", None))
        if not POLICY.gpu_snapshot:
            self._start_engine()
        for port, proc in zip(COMFY_PORTS, self.procs):
            wait_for_server(port, proc, timeout=30)
        if DELIVERY_MODE == "legacy":
            self.sync_proc = subprocess.Popen(
                ["python", "-u", "/root/hf_sync.py"],
                env={**os.environ, "WORKFLOW": WORKFLOW, "COMFY_OUTPUT_ROOT": f"/outputs/{WORKFLOW}"},
                start_new_session=True,
            )
        # Start only after snapshot restore: background threads must not enter snapshots.
        import threading
        self.commit_lock = threading.Lock()
        self.commit_stop = threading.Event()
        self.commit_thread = threading.Thread(target=self._commit_outputs_loop, daemon=True)
        self.commit_thread.start()
        log_event("model_ready", workflow=WORKFLOW,
                  capture_id=getattr(self, "snapshot_capture_id", None),
                  engine_seconds=self.engine_initialization_seconds,
                  post_hook_seconds=time.monotonic()-started,
                  region=os.getenv("MODAL_REGION"), provider=os.getenv("MODAL_CLOUD_PROVIDER"),
                  deployment_version=DEPLOYMENT_VERSION, delivery=DELIVERY_MODE)

    if DELIVERY_MODE == "cpu":
        @modal.method()
        def publish_outputs(self, prompt_id: str, instance: int = 1, expected_version: str | None = None):
            """Authenticated publication of a successful native ComfyUI prompt."""
            import urllib.request
            from storage.delivery import instance_number
            token(prompt_id, "prompt ID")
            instance_number(instance)
            if instance > len(COMFY_PORTS):
                raise ValueError("Invalid ComfyUI instance")
            if expected_version is not None and expected_version != DEPLOYMENT_VERSION:
                raise RuntimeError("Deployment version changed")
            config = {"app": APP_NAME, "workflow": WORKFLOW, "instance": instance,
                      "volume": OUTPUT_VOLUME_NAME, "version": DEPLOYMENT_VERSION}
            root = Path("/outputs")
            started = time.monotonic()
            with self.commit_lock:
                ready = root / receipt_key(WORKFLOW, instance, prompt_id)
                if ready.is_file():
                    receipt = validate_receipt(json.loads(ready.read_text(encoding="utf-8")),
                                               config, prompt_id)
                else:
                    url = f"http://127.0.0.1:{COMFY_PORTS[instance-1]}/history/{prompt_id}"
                    with urllib.request.urlopen(url, timeout=10) as response:
                        history = json.load(response)
                    if prompt_id not in history:
                        raise FileNotFoundError("Prompt history not available; do not resubmit automatically")
                    receipt = build_receipt(root, config, prompt_id, history[prompt_id],
                                            dual=DUAL_COMFYUI, version=DEPLOYMENT_VERSION)
                receipt = publish_receipt(root, output_volume, receipt)
            log_event("delivery_ready", prompt_id=prompt_id, files=len(receipt["files"]),
                      seconds=time.monotonic()-started)
            return receipt
    
    @modal.web_server(8000, startup_timeout=1800 if WORKFLOW == "010-minimax-h3-b200plus-suite" else 300, label=("comfyui-h3-int8" if APP_NAME == f"modal-{WORKFLOW}" and WORKFLOW == "010-minimax-h3-b200plus-suite"
                       else WEB_LABEL_PREFIX + "-1"))
    def ui(self):
        print("App Ready!")

    if DUAL_COMFYUI:
        @modal.web_server(8001, startup_timeout=300, label=WEB_LABEL_PREFIX + "-2")
        def ui_second(self):
            print("ComfyUI instance 2 ready!")

    @modal.exit()
    def cleanup(self):
        started = time.monotonic()
        log_event("shutdown_begin")
        errors = []
        commit_stop = getattr(self, "commit_stop", None)
        if commit_stop is not None:
            commit_stop.set()
        # Stop all producers before the final staging commit, even if one
        # producer fails to terminate. Preserve Volume files for future archive.
        procs = [(f"comfy-{i}", proc) for i, proc in enumerate(getattr(self, "procs", []), 1)]
        sync = getattr(self, "sync_proc", None)
        if sync is not None:
            procs.append(("hf-sync", sync))
        for name, proc in procs:
            try:
                stop_process(proc, name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                log_event("shutdown_process_error", process=name, error=str(exc))
        thread = getattr(self, "commit_thread", None)
        if thread is not None:
            thread.join(timeout=10)
        if thread is not None and thread.is_alive():
            # Never run a second commit concurrently with a blocked writer.
            errors.append("background Volume commit still running")
        if errors:
            log_event("shutdown_commit_deferred", reason="; ".join(errors))
        else:
            commit_started = time.monotonic()
            try:
                output_volume.commit()
                log_event("shutdown_commit", seconds=time.monotonic()-commit_started)
            except Exception as exc:
                errors.append(str(exc))
                log_event("shutdown_commit_error", error=str(exc))
        log_event("shutdown_end", seconds=time.monotonic()-started, errors=errors)
        if errors:
            raise RuntimeError("; ".join(errors))

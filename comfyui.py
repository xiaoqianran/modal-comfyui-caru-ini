from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import modal

import json

# Modal 镜像构建与应用声明顺序（必须保持）：
# 1. 安装 ComfyUI。
# 2. 下载模型（构建时使用 models.json / plugins.json）。
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
if WORKFLOW not in {p.name for p in (root_dir / "workflows").iterdir() if p.is_dir()}:
    raise ValueError(f"Unknown workflow: {WORKFLOW}")

model_config = json.loads((WORKFLOW_DIR / "models.json").read_text(encoding="utf-8"))
plugin_config = json.loads((WORKFLOW_DIR / "plugins.json").read_text(encoding="utf-8"))
models = model_config["models"]
models_ext = model_config.get("models_ext", [])
comfy_plugins = plugin_config.get("comfy_plugins", [])
comfy_plugins_ext = plugin_config.get("comfy_plugins_ext", [])
# Workflow files are intentionally not validated at module import time:
# Modal re-imports this module during download_all before workflow mounts exist.

COMFY_MODELS_ROOT = Path("/root/comfy/ComfyUI/models")

GPU_TYPE = os.getenv("MODAL_GPU") or model_config.get("gpu", "L40S")
COMFY_VER = os.getenv("COMFY_VER")

# ----------------------------------------------------------------------------
# 模型下载资源（动态调配）：默认就是快档 CPU=16 / MEMORY=32768 MiB，
# 不设环境变量直接部署即按快档跑；仍可用环境变量继续调高/调低，例如
#   $env:MODEL_DOWNLOAD_CPU="32"; $env:MODEL_DOWNLOAD_MEMORY="65536"
#   $env:MODEL_DOWNLOAD_WORKERS="10"; .\deploy-modal.bat community-minimaxh3-studio
# 只影响构建期下载模型的那个容器，不影响 ComfyUI GPU 运行容器的资源。
# MODEL_DOWNLOAD_WORKERS=0（默认）为自适应：min(模型文件数, CPU 核数)。
# 这三个值会通过 run_function 的 env 参数原样传进远程构建容器，保证本地
# 解析值与远程 download_all() 重导入时读到的值一致；download_all() 直接
# 引用这些全局变量，改动它们也会触发 Modal 重建下载层。
# ----------------------------------------------------------------------------
def _env_number(name: str, default, lo, hi, cast):
    raw = os.getenv(name, "").strip()
    if not raw:
        return cast(default)
    try:
        value = cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


DOWNLOAD_CPU = _env_number("MODEL_DOWNLOAD_CPU", 16.0, 0.25, 128, float)
DOWNLOAD_MEMORY = _env_number("MODEL_DOWNLOAD_MEMORY", 32768, 512, 1048576, int)
DOWNLOAD_WORKERS = _env_number("MODEL_DOWNLOAD_WORKERS", 0, 0, 32, int)
DOWNLOAD_TIMEOUT = _env_number("MODEL_DOWNLOAD_TIMEOUT", 7200, 60, 86400, int)

# ============================================================================
# 2. 准备模型下载函数：定位目标目录，从 HF / 外部地址下载并建立链接
#    这里定义函数；实际下载在第 4 阶段构建镜像时执行。
# ============================================================================
def resolve_model_dir(model_dir: str) -> Path:
    """Resolve model_dir: absolute paths are used as-is, relative paths are
    placed under /root/comfy/ComfyUI/models/ (e.g. "checkpoints")."""
    p = Path(model_dir)
    return p if p.is_absolute() else COMFY_MODELS_ROOT / p


def hf_download(
    repo_id: str,
    filename: str,
    model_dir: str = "checkpoints",
):
    import os
    import subprocess

    # Download model from Hugging Face
    from huggingface_hub import hf_hub_download

    model = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        cache_dir="/cache",
        token=os.environ.get("HF_TOKEN"),
    )

    target_dir = resolve_model_dir(model_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    local_filename = next((m.get("local_filename") for m in models if m["repo_id"] == repo_id and m["filename"] == filename), None) or Path(filename).name
    target_path = target_dir / local_filename

    # Remove existing file/link if it exists to ensure fresh link
    if target_path.exists() or target_path.is_symlink():
        target_path.unlink()

    # Create symlink
    target_path.symlink_to(model)
    print(f"Downloaded {repo_id}/{filename} to {target_path}")


def download_external_model(url: str, filename: str, model_dir: str):
    import subprocess

    cache_dir = "/cache"
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    cached_path = Path(cache_dir) / filename
    if not cached_path.exists():
        print(f"Downloading {filename} from {url}...")
        # Pass the CivitAI token as a query param rather than an Authorization
        # header: axel re-sends -H headers to the redirect target (an R2
        # presigned URL), which rejects them with 400.
        download_url = url
        if url.startswith(("https://civitai.com/", "https://civitai.red/")):
            token = os.environ.get("CIVITAI_TOKEN", "")
            if token:
                parts = urlsplit(url)
                query = dict(parse_qsl(parts.query))
                query.setdefault("token", token)
                download_url = urlunsplit(parts._replace(query=urlencode(query)))

        try:
            _ = subprocess.run(
                [
                    "axel",
                    "-n", "16",
                    "-o", cached_path,
                    download_url,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            print("STDERR:", e.stderr)
            raise

    target_dir = resolve_model_dir(model_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    # Remove existing file/link if it exists to ensure fresh link
    if target_path.exists() or target_path.is_symlink():
        target_path.unlink()

    # Create symlink
    target_path.symlink_to(cached_path)
    print(f"Linked {filename} to {target_path}")


def download_all():
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep, time

    def fetch(item):
        kind, model = item
        for attempt in range(3):
            try:
                if kind == "hf":
                    hf_download(model["repo_id"], model["filename"], model["model_dir"])
                else:
                    download_external_model(model["url"], model["filename"], model["model_dir"])
                return
            except Exception:
                if attempt == 2:
                    raise
                sleep(2 ** attempt)

    items = [("hf", model) for model in models]
    items += [("external", model) for model in models_ext]
    # 动态线程数：显式设置 MODEL_DOWNLOAD_WORKERS 时用设置值（1-32）；
    # 为 0 时自适应为 min(文件数, CPU 核数)，且永远不超过文件数。
    if DOWNLOAD_WORKERS > 0:
        workers = DOWNLOAD_WORKERS
    else:
        workers = min(len(items) or 1, max(1, int(DOWNLOAD_CPU)))
    workers = min(workers, len(items) or 1)
    print(
        f"Downloading {len(items)} model files: workers={workers}, "
        f"cpu={DOWNLOAD_CPU}, memory={DOWNLOAD_MEMORY} MiB"
    )
    started = time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch, items))
    print(f"All model downloads finished in {time() - started:.1f}s")


# ============================================================================
# 3. 构建 Modal 基础镜像：选择 CUDA / Python，安装系统依赖与 ComfyUI
#    模型与插件暂不在此层加入，便于复用基础镜像缓存。
# ============================================================================
# 模型缓存卷用 Volumes v2（官方推荐：吞吐更高、并发写更好）。
# 注意：已有的 v1 卷 "modal-comfyui-models" 不能原地升级，且 from_name 的
# version 必须与已有卷一致、同名传 version=2 会直接报错，所以 v2 用新卷名
# "modal-comfyui-models-v2"；切换后首次部署会重新下载一次模型（旧 v1 卷
# 数据保留不动，确认无用后可自行在 Modal 控制台删除）。
vol = modal.Volume.from_name("modal-comfyui-models-v2", create_if_missing=True, version=2)

# construct images and install deps/custom nodes
VERSION = f"--version {COMFY_VER}" if COMFY_VER else ""
# Unified CUDA 13.0 / PyTorch 2.14.1 image for every GPU type.
image = (
    modal.Image.from_registry("nvidia/cuda:13.0.3-devel-ubuntu22.04", add_python="3.11")
    .entrypoint([])
    .apt_install("git", "git-lfs", "libgl1-mesa-dev", "libglib2.0-0", "axel", "build-essential", "ninja-build")
    .pip_install_from_requirements(str(root_dir / "requirements_comfy.txt"))
    .run_commands(
        "uv pip install --system "
        "torch==2.14.1 torchvision==0.29.1 "
        "--index-url https://download.pytorch.org/whl/cu130"
    )
    .run_commands(f"comfy --skip-prompt install --nvidia --skip-torch-or-directml {VERSION}")
    .run_commands("git lfs install")
)
# ============================================================================
# 4. 下载模型权重：获取 HF 凭据，在构建阶段写入共享 Volume 并建立链接
# ============================================================================
def _hf_secrets() -> list[modal.Secret]:
    """Prefer Modal Secret 'huggingface-secret'; fall back to local HF_TOKEN
    env. Public models work even when both are absent (warned)."""
    try:
        s = modal.Secret.from_name("huggingface-secret")
        s.hydrate()  # from_name is lazy, force the existence check here
        return [s]
    except modal.exception.NotFoundError:
        hf_token = os.environ.get("HF_TOKEN", "")
        civ_token = os.environ.get("CIVITAI_TOKEN", "")
        if not hf_token:
            print(
                "Warning: no Modal Secret 'huggingface-secret' and no HF_TOKEN env. "
                "Public models will download with throttled bandwidth; "
                "gated models will fail."
            )
        return [modal.Secret.from_dict({"HF_TOKEN": hf_token, "CIVITAI_TOKEN": civ_token})]

# download models
# run_function imports comfyui.py inside the image: its selected Workflow
# config must already exist there (without copying unrelated workflows).
for filename in ("models.json", "plugins.json"):
    image = image.add_local_file(
        WORKFLOW_DIR / filename, f"/root/workflows/{WORKFLOW}/{filename}", copy=True
    )
# 只开 Xet 高性能模式：HF_HUB_ENABLE_HF_TRANSFER 已被 Hugging Face 废弃
# （hf_transfer 不再可用），不要再设置它，避免下载路径含糊。
image = image.env(
    {"HF_XET_HIGH_PERFORMANCE": "1", "WORKFLOW": WORKFLOW}
).run_function(
    download_all,
    cpu=DOWNLOAD_CPU,
    memory=DOWNLOAD_MEMORY,
    timeout=DOWNLOAD_TIMEOUT,
    env={
        "MODEL_DOWNLOAD_CPU": str(DOWNLOAD_CPU),
        "MODEL_DOWNLOAD_MEMORY": str(DOWNLOAD_MEMORY),
        "MODEL_DOWNLOAD_WORKERS": str(DOWNLOAD_WORKERS),
        "MODEL_DOWNLOAD_TIMEOUT": str(DOWNLOAD_TIMEOUT),
    },
    volumes={"/cache": vol},
    secrets=_hf_secrets(),
)


# ============================================================================
# 5. 插件由 plugins.json 声明；工作流在全部构建步骤完成后挂载
# ============================================================================
if comfy_plugins:
    image = image.run_commands("comfy node install " + " ".join(comfy_plugins))


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
                    "from importlib.metadata import version, PackageNotFoundError; "
                    "from pathlib import Path; "
                    "packages=('torch','torchvision','torchaudio'); "
                    "installed=[(p, version(p)) for p in packages]; "
                    f"Path('{constraints}').write_text(''.join(p+'=='+v+chr(10) for p,v in installed))"
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



# ============================================================================
# Qwen-Image-2.1: fix missing os.memfd_create in ComfyUI only, without PYTHONPATH overrides.
if WORKFLOW == "comfyui-qwen-image-2-1":
    image = image.add_local_file(
        root_dir / "testing" / "qwen21_patch_memfd.py",
        "/root/qwen21_patch_memfd.py",
        copy=True,
    ).run_commands("python /root/qwen21_patch_memfd.py")

# 6. Output staging Volume; R2 only stores small previews
#    wait_for_port 用于后续 ComfyUI 启动时检查 HTTP 服务是否就绪。
# ============================================================================
def wait_for_port(port: int, timeout: int = 60):
    """Block until the port is accepting connections."""
    import time
    import socket
    
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return  # port is open — ComfyUI is ready
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"ComfyUI never became ready on port {port}")
    
# Generated files stay in a persistent staging Volume until HF archive succeeds.
output_volume = modal.Volume.from_name("modal-comfyui-models-output", create_if_missing=True)
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
image = image.pip_install("boto3", "imageio-ffmpeg").add_local_file(
    root_dir / "storage" / "hf_sync.py", "/root/hf_sync.py", copy=True
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
app = modal.App(name=f"modal-{WORKFLOW}", image=image)


@app.cls(
    max_containers=1,
    gpu=GPU_TYPE,
    volumes={
        "/cache": vol,
        "/outputs": output_volume,
        "/r2-previews": preview_mount,
    },
    secrets=[modal.Secret.from_name("huggingface-s3")],
    scaledown_window=60,  # idle 1 minutes to shutdown
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
# ============================================================================
# 8. ComfyUI 生命周期：启动 Web 服务、恢复快照、执行退出清理
# ============================================================================
@modal.concurrent(max_inputs=10)
class ComfyUI:
    @modal.enter(snap=True)
    def start_checkpoint(self):
        self.proc = subprocess.Popen(
            f"comfy launch --background -- --listen 0.0.0.0 --port 8000 --output-directory /outputs/{WORKFLOW}", shell=True
        )
        # Block here — snapshot is taken only after this returns
        wait_for_port(8000, timeout=300)

    @modal.enter(snap=False)
    def start_restore(self):
        wait_for_port(8000, timeout=30)
        self.sync_proc = subprocess.Popen(
            ["python", "-u", "/root/hf_sync.py"],
            env={**os.environ, "WORKFLOW": WORKFLOW, "COMFY_OUTPUT_ROOT": f"/outputs/{WORKFLOW}"},
        )
        print("App Restored!")
    
    @modal.web_server(8000, startup_timeout=300)
    def ui(self):
        print("App Ready!")
    
    @modal.exit()
    def cleanup(self):
        # Persist staging files even if HF was temporarily unavailable.
        output_volume.commit()
        sync_proc = getattr(self, "sync_proc", None)
        if sync_proc is not None:
            sync_proc.terminate()
            try:
                sync_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sync_proc.kill()
        proc = getattr(self, "proc", None)
        if proc is not None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        print("App CleanUp!")

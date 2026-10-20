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
    from time import sleep

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
    workers = int(os.getenv("MODEL_DOWNLOAD_WORKERS", "4"))
    if not 1 <= workers <= 6:
        raise ValueError("MODEL_DOWNLOAD_WORKERS must be between 1 and 6")
    with ThreadPoolExecutor(max_workers=min(workers, len(items) or 1)) as pool:
        list(pool.map(fetch, items))


# ============================================================================
# 3. 构建 Modal 基础镜像：选择 CUDA / Python，安装系统依赖与 ComfyUI
#    模型与插件暂不在此层加入，便于复用基础镜像缓存。
# ============================================================================
vol = modal.Volume.from_name("hf-hub-cache", create_if_missing=True)

# construct images and install deps/custom nodes
VERSION = f"--version {COMFY_VER}" if COMFY_VER else ""
image = (
    (modal.Image.from_registry("nvidia/cuda:12.6.3-devel-ubuntu22.04", add_python="3.11") if model_config.get("cuda_devel") else modal.Image.debian_slim(python_version="3.11"))
    .apt_install("git", "git-lfs", "libgl1-mesa-dev", "libglib2.0-0", "axel", "build-essential", "ninja-build")
    .pip_install_from_requirements(str(root_dir / "requirements_comfy.txt"))
    .run_commands(f"comfy --skip-prompt install --nvidia {VERSION}")
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
image = image.env(
    {"HF_HUB_ENABLE_HF_TRANSFER": "1", "HF_XET_HIGH_PERFORMANCE": "1", "WORKFLOW": WORKFLOW}
).run_function(download_all, volumes={"/cache": vol}, secrets=_hf_secrets())


# ============================================================================
# 5. 插件由 plugins.json 声明；工作流在全部构建步骤完成后挂载
# ============================================================================
if WORKFLOW == "community-hunyuan3d-2-1-visualbruno":
    # The bundled DINOv2 checkpoint uses Transformers 4.x state_dict names.
    # ComfyUI Manager installs unpinned Transformers; v5 renames these keys.
    image = image.run_commands(
        "python -m pip install 'transformers==4.49.0' 'diffusers==0.35.2' && "
        "python -c 'import transformers; assert transformers.__version__ == \"4.49.0\"'"
    )

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
            image = image.run_commands(f"cd {work_dir} && python {shlex.quote(install)}")
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
# 6. 配置输出存储：挂载生成资产目录 output/3d，准备关联的存储依赖
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
    
r2_mount = modal.CloudBucketMount(
    bucket_name="modal-studio-assets",
    bucket_endpoint_url=os.getenv(
        "R2_ENDPOINT", "https://f863e42ff2096d089d4d4129197abd17.r2.cloudflarestorage.com"
    ),
    key_prefix=f"comfyui/{WORKFLOW}/3d/",
    secret=modal.Secret.from_name(
        "cloudflare-r2",
        required_keys=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
    ),
)
image = image.pip_install("boto3").add_local_file(
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
        "/root/comfy/ComfyUI/output/3d": r2_mount,
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
            "comfy launch --background -- --listen 0.0.0.0 --port 8000", shell=True
        )
        # Block here — snapshot is taken only after this returns
        wait_for_port(8000, timeout=300)

    @modal.enter(snap=False)
    def start_restore(self):
        wait_for_port(8000, timeout=30)
        self.sync_proc = subprocess.Popen(
            ["python", "-u", "/root/hf_sync.py"],
            env={**os.environ, "WORKFLOW": WORKFLOW},
        )
        print("App Restored!")
    
    @modal.web_server(8000, startup_timeout=300)
    def ui(self):
        print("App Ready!")
    
    @modal.exit()
    def cleanup(self):
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

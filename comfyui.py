from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import modal

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
HIGH_VRAM = model_config.get("highvram", IS_BLACKWELL or GPU_TYPE.strip().upper() == "H200")
DUAL_COMFYUI = GPU_TYPE.strip().upper() == "B300"
COMFY_PORTS = (8000, 8001) if DUAL_COMFYUI else (8000,)
COMFY_VER = os.getenv("COMFY_VER")

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
    revision_file = Path("/cache/.workflow-revisions") / f"{WORKFLOW}.json"
    revisions = json.loads(revision_file.read_text()) if revision_file.is_file() else {}

    for model in models:
        key = f"{model['repo_id']}/{model['filename']}"
        revision = revisions.get(key)
        try:
            cached = hf_hub_download(
                repo_id=model["repo_id"], filename=model["filename"],
                cache_dir="/cache", revision=revision, local_files_only=True)
        except LocalEntryNotFoundError as exc:
            # Compatibility for older caches without an exact revision manifest.
            repo_cache = Path("/cache") / ("models--" + model["repo_id"].replace("/", "--"))
            candidates = list((repo_cache / "snapshots").glob("*/" + model["filename"]))
            candidates = [path for path in candidates if path.is_file()]
            if revision is None and len(candidates) == 1:
                cached = str(candidates[0])
            else:
                raise RuntimeError(
                    f"Model not cached: {key} (revision={revision!r}). "
                    "Run: uv run modal run comfyui.py::download_models") from exc
        name = model.get("local_filename") or Path(model["filename"]).name
        target = resolve_model_dir(model["model_dir"]) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.symlink_to(cached)
        print(f"CACHE HIT {key} revision={revision or 'legacy'}")
    for model in models_ext:
        cached = Path("/cache") / model["filename"]
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
# 模型缓存卷用 Volumes v2（官方推荐：吞吐更高、并发写更好）。
# 注意：已有的 v1 卷 "modal-comfyui-models" 不能原地升级，且 from_name 的
# version 必须与已有卷一致、同名传 version=2 会直接报错，所以 v2 用新卷名
# "modal-comfyui-models-v2"；切换后首次部署会重新下载一次模型（旧 v1 卷
# 数据保留不动，确认无用后可自行在 Modal 控制台删除）。
vol = modal.Volume.from_name("modal-comfyui-models-v2", create_if_missing=True, version=2)

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
    .pip_install_from_requirements(str(root_dir / "requirements_comfy.txt"))
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
output_volume = modal.Volume.from_name("modal-comfyui-models-output", create_if_missing=True, version=2)
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
).env({"WORKFLOW": WORKFLOW})

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
app = modal.App(name=f"modal-{WORKFLOW}", image=image)


@app.local_entrypoint()
def download_models():
    """Use the canonical 16GB-sharded downloader; do not launch a GPU."""
    # The standalone downloader owns shard packing, retry, verification and
    # Volume V2 merge. This keeps both CLI entrypoints on exactly one path.
    subprocess.run(
        ["uv", "run", "modal", "run", "download-models.py"],
        cwd=root_dir,
        env=os.environ.copy(),
        check=True,
    )


@app.cls(
    max_containers=1,  # Hard cap: never start a second B300 container for this app.
    gpu=GPU_TYPE,
    region=MODAL_REGIONS,
    volumes={
        "/cache": vol,
        "/outputs": output_volume,
        "/r2-previews": preview_mount,
    },
    secrets=[modal.Secret.from_name("huggingface-s3")],
    # 所有已部署的工作流保持一个 GPU 容器常驻，不因空闲缩容至零。
    min_containers=1,
    scaledown_window=3600 if WORKFLOW == "comfyui-b300-image-video-suite" else 1800,
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
        # Reload the shared model Volume before resolving newly committed weights.
        import time
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
        self.procs = []
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
            self.procs.append(subprocess.Popen(args))
        for port in COMFY_PORTS:
            wait_for_port(port, timeout=300)

    @staticmethod
    def _output_snapshot():
        """Track completed-looking output files without touching temp files."""
        extensions = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz",
                      ".mp4", ".webm", ".mov", ".mkv", ".png", ".jpg", ".jpeg",
                      ".json", ".bin", ".obj", ".mtl", ".stl"}
        root = Path("/outputs") / WORKFLOW
        snapshot = {}
        if not root.is_dir():
            return snapshot
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
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
                    output_volume.commit()
                    committed = stable
                    print(f"Output Volume V2 committed: {len(stable)} stable files", flush=True)
                previous = current
            except Exception as exc:
                print(f"Output Volume V2 commit failed (will retry): {exc}", flush=True)

    @modal.enter(snap=False)
    def start_restore(self):
        print(f"GPU Region: {os.getenv('MODAL_REGION', 'unknown')}", flush=True)
        print(f"Cloud Provider: {os.getenv('MODAL_CLOUD_PROVIDER', 'unknown')}", flush=True)
        for port in COMFY_PORTS:
            wait_for_port(port, timeout=30)
        self.sync_proc = subprocess.Popen(
            ["python", "-u", "/root/hf_sync.py"],
            env={**os.environ, "WORKFLOW": WORKFLOW, "COMFY_OUTPUT_ROOT": f"/outputs/{WORKFLOW}"},
        )
        # Start only after snapshot restore: background threads must not enter snapshots.
        import threading
        self.commit_stop = threading.Event()
        self.commit_thread = threading.Thread(target=self._commit_outputs_loop, daemon=True)
        self.commit_thread.start()
        print("App Restored!")
    
    @modal.web_server(8000, startup_timeout=300, label="comfyui-1")
    def ui(self):
        print("App Ready!")

    if DUAL_COMFYUI:
        @modal.web_server(8001, startup_timeout=300, label="comfyui-2")
        def ui_second(self):
            print("ComfyUI instance 2 ready!")

    @modal.exit()
    def cleanup(self):
        # Stop background commits before the final flush on graceful shutdown.
        commit_stop = getattr(self, "commit_stop", None)
        if commit_stop is not None:
            commit_stop.set()
            self.commit_thread.join(timeout=10)
        # Persist staging files even if HF was temporarily unavailable.
        output_volume.commit()
        sync_proc = getattr(self, "sync_proc", None)
        if sync_proc is not None:
            sync_proc.terminate()
            try:
                sync_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                sync_proc.kill()
        for proc in getattr(self, "procs", []):
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        print("App CleanUp!")

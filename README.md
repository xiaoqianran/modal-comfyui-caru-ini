# modal-comfyui-caru-ini

基于 [caru-ini/modal-comfyui](https://github.com/caru-ini/modal-comfyui) 的 Fork 扩展项目。感谢原作者 **caru-ini** 开源 Modal + ComfyUI 的部署方案、自动扩缩容、模型缓存与自定义节点管理能力。本仓库在原项目基础上继续开发；原项目与本仓库的后续改动请分别查看各自的 Git 历史。

This project builds on [caru-ini/modal-comfyui](https://github.com/caru-ini/modal-comfyui). Thanks to **caru-ini** for the original open-source Modal + ComfyUI implementation. This fork adds workflow-oriented deployment and asset management while preserving the upstream foundation.

## 本 Fork 新增功能

- **多工作流配置**：每个 `workflows/<name>/` 使用 API Workflow JSON、`models.json`、`plugins.json`；工作流独立 Modal App。
- **工作流部署管理**：通用部署 BAT；本地 `frontend/` 管理界面可查看状态、触发部署或取消部署。
- **统一资产浏览**：`storage/s3.py` 访问 Cloudflare R2 与 Hugging Face Storage Buckets；预览 GLB 和 Gaussian Splat 文件。
- **生成资产同步**：R2 挂载 `output/3d`，`storage/hf_sync.py` 后台将生成文件复制到 HF S3。
- **GPU 与自定义 CUDA 节点**：各工作流独立配置 GPU，支持额外编译命令。

> 本项目是社区 Fork，并非原作者的官方发布。部分工作流依赖第三方自定义节点和云端凭据；部署前请自行核对依赖与权限。

## Prerequisites

- A Modal account
- Python installed
- `uv` installed

## Installation

1. Clone this repository.
2. Install the Modal client:
   ```bash
   uv sync
   ```
3. Set up your modal account (if not done already):
   ```bash
   modal setup
   ```
4. Set up your modal secret (optional; recommended)
   ```bash
   modal secret create huggingface-secret HF_TOKEN=hf_[your HF access token]
   ```

## Configuration

### Workflow configuration

Each `workflows/<name>/` defines one ComfyUI deployment with `models.json` and `plugins.json`. Multiple UI workflow JSONs are allowed, including nested directories; deployment mounts these files recursively. The Task Runner separately requires a **ComfyUI API-format JSON**. Do not mix unrelated test JSONs into an active workflow directory.

#### `models.json`

```json
{
  "models": [
    {
      "repo_id": "tencent/Hunyuan3D-2.1",
      "filename": "hunyuan3d-dit-v2-1/model.fp16.ckpt",
      "model_dir": "diffusion_models",
      "local_filename": "hunyuan3d-dit-v2-1-fp16.ckpt"
    }
  ],
  "models_ext": [],
  "gpu": "L40S"
}
```

- `models`: Hugging Face Hub models. Each requires `repo_id`, `filename`, and `model_dir`; `local_filename` optionally renames the linked file.
- `models_ext`: external direct downloads. Each requires `url`, `filename`, and `model_dir`; use `[]` if none.
- `model_dir`: relative to `/root/comfy/ComfyUI/models/`, or an absolute destination.
- `gpu`: Modal GPU specification; `MODAL_GPU` overrides it. GPU type varies by workflow.
- GPU-specific CUDA/PyTorch versions are currently selected in `comfyui.py`, not by `cuda_devel`.

#### `plugins.json`

```json
{
  "comfy_plugins": [],
  "comfy_plugins_ext": []
}
```

- `comfy_plugins`: optional list of node names/IDs for `comfy node install`.
- `comfy_plugins_ext`: optional list of Git custom nodes. `url` is required; `branch`, `commit`, `requirements`, `install` (Python script), `ext_deps`, `build_commands`, `wheels`, and `wheel_torch` are optional.
- `requirements` and `ext_deps` are installed using `--no-deps`; `build_commands` runs as shell commands in the cloned plugin directory. Only configure trusted commands.
- For no extra plugins use `{"comfy_plugins": [], "comfy_plugins_ext": []}`. The deployment still runs `comfy node install-deps` on the API graph.


## Usage

### Serve (Development)

Run the following command to start ComfyUI in development mode:
```bash
modal serve comfyui.py
```
This will provide a temporary URL where you can access the ComfyUI interface.

### Deploy (Production)

To deploy a specific workflow (Windows):
```powershell
.\deploy-modal.bat comfyui-pixal3d-trellis2
```

Alternatively set `WORKFLOW` in the environment and run `uv run modal deploy comfyui.py`.


## Windows / Linux 命令行操作速查

所有命令均在仓库根目录执行。工作流名称是 `workflows/` 下的**目录名**（不是其中的 JSON 文件名）。下面以 `comfyui-pixal3d-trellis2` 为例；社区版可替换为 `community-pixal3d-trellis2-tuning`。

### 0. 环境初始化（两平台通用）

```bash
uv sync
uv run modal setup
uv run modal profile current
```

在部署前检查 `uv run modal profile current` 的账户与环境；Modal Secrets 属于指定账户和环境（如 `main`），不会跨账户自动共享。将 `.env.example` 复制为 `.env`，填入真实密钥；`.env` 不要提交到 Git。

### 1. 上传 / 初始化 Modal Secrets（只需首次配置）

**Windows（PowerShell）：**

```powershell
Copy-Item .env.example .env  # 若已有 .env，跳过本行，避免覆盖
# 编辑 .env，填入密钥
.\setup-secrets.bat
uv run modal secret list
```

**Linux（Bash）：**

```bash
cp -n .env.example .env    # 若已有 .env，不覆盖
# 编辑 .env，填入密钥
bash setup-secrets.sh
uv run modal secret list
```

两个脚本只创建缺失的 Secrets，不会覆盖既有云端密钥：

| Modal Secret | 本地 .env 变量 | 说明 |
| --- | --- | --- |
| `huggingface-s3` | `HF_S3_ACCESS_KEY_ID`、`HF_S3_SECRET_ACCESS_KEY` | 必需；上传时映射成 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` |
| `cloudflare-r2` | `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` | 必需；R2 挂载 |
| `huggingface-secret` | `HF_TOKEN` | 可选；私有 / gated 模型需要有效授权 |
| `github` | `GITHUB_TOKEN` | 可选 |

这些 Secrets 创建在 Modal 云端后会保留，**不是每次部署都要重新上传**。更换密钥时需要主动更新云端 Secret，而不是仅修改本地 `.env`。

### 2. 仅下载模型（不部署 ComfyUI）

两个入口 `download-models.py` 与 `comfyui.py::download_models` 使用同一个分片下载实现，默认 16 GB/片、最多 16 容器、每容器 4 CPU / 4 GiB / 2 线程，关闭 Xet 高性能模式。`comfyui.py::download_models` 是本地入口，委托运行 `download-models.py`，不构建或启动 GPU 推理服务。

独立下载入口 `download-models.py` 读取 `workflows/<名称>/models.json`，将 HF 文件和直链文件下载至共享 Modal Volume `modal-comfyui-models-v2`（Volumes v2；旧 v1 卷 `modal-comfyui-models` 不能原地升级，已弃用）。不会启动 ComfyUI UI，也不部署工作流服务。会产生 Modal CPU 和下载 / 存储费用。下载完成后，正常部署仍需构建镜像和安装节点，但相同模型文件可复用缓存。

**Windows（PowerShell）：**

```powershell
$env:WORKFLOW = "comfyui-pixal3d-trellis2"
uv run --env-file .env modal run download-models.py
```

**Linux（Bash）：**

```bash
WORKFLOW=comfyui-pixal3d-trellis2 uv run --env-file .env modal run download-models.py
```

该脚本的 Modal CPU 镜像已安装 `hf_xet`，支持 Hugging Face Xet 下载；如果正在执行的旧容器出现 `hf_xet package is not installed`，属于性能警告，不是下载失败，新代码只会在下一次运行时生效。该脚本优先使用云端 `huggingface-secret`，未创建时使用本地 `HF_TOKEN`。针对需要 CivitAI 认证或自定义认证头的直链模型，请先确认授权及下载方式；当前独立下载入口只支持普通 HTTP(S) 直链。模型缓存位于云端 Volume，而不是 Linux/Windows 本地磁盘。

### 3. 部署指定工作流

**Windows（PowerShell）：**

```powershell
.\deploy-modal.bat comfyui-pixal3d-trellis2
# 或：
$env:WORKFLOW = "comfyui-pixal3d-trellis2"
uv run modal deploy comfyui.py
```

**Linux（Bash）：**

```bash
WORKFLOW=comfyui-pixal3d-trellis2 uv run modal deploy comfyui.py
# 如果 WORKFLOW 已正确写入 .env：
uv run --env-file .env modal deploy comfyui.py
```

`MODAL_GPU` 可覆盖该工作流 `models.json` 中的 GPU 设置。在 Linux 的 `.env` 中应写 `WORKFLOW=comfyui-pixal3d-trellis2`，不要写 PowerShell 的 `$env:WORKFLOW=...`。普通 `uv run modal deploy comfyui.py` **不会自动读取 .env**；必须通过 `--env-file .env` 或提前导出变量。部署会使用已创建的云端 Secrets，不会替你上传或更新这些 Secrets。

### 4. 获取 ComfyUI 访问地址

`ComfyUI.ui` 是 Modal Class 的 Web 方法，Modal Dashboard 的 App 概览不会显示它的 URL（这不是部署失败）。地址格式固定：

```
https://<workspace>--modal-<workflow>-comfyui-ui.<region>.modal.run/
```

本地控制台（`start-frontend.bat`）会读取当前 Modal profile 的 workspace，在「工作流」页自动拼出该地址，并提供「打开」和「复制」按钮。域名标签超过 63 个字符时 Modal 会截断，此时页面会提示核对 Dashboard。

### 5. 检查部署

```bash
uv run modal app list
uv run modal secret list
```

## Features

- **Auto-scaling**: Scales down to zero when not in use to save costs.
- **GPU Snapshots**: Fast startup times using Modal's GPU snapshots.
- **Model Caching**: Uses Modal Volumes to cache models across runs.
- **Custom Node Management**: Integrated with `comfy-cli` for easy plugin installation.

## 输出持久化与对象存储

ComfyUI 保存到 Modal Volume V2 的 `/outputs/<workflow>/`。单实例可以直接生成图片或按 `video/`、`3d/` 等子目录保存；B300 双实例分别保存至 `instance-1/`、`instance-2/`。

`storage/hf_sync.py` 定期扫描稳定文件，按 `comfyui/<workflow>/<image|video|3d|3dgs>/...` 归档到 Hugging Face Storage Bucket。双实例的实例名保留在类别目录之后，例如 `comfyui/<workflow>/video/instance-1/clip.mp4`。云端对象的 SHA-256 元数据用于核对内容；旧对象缺少校验元数据时，会重新上传一次。

Cloudflare R2 仅保存图片和视频的预览，以及用户请求下载时的临时副本。原始文件以 HF 存储为准。详细路径与注意事项参见 [storage/README.md](storage/README.md)。

## Local console

Run `uv run python frontend/server.py` and open http://127.0.0.1:8768 .
The console manages Modal workflows and previews private R2 / Hugging Face S3
assets. It binds only to localhost and reads local credentials from `.env`.
The original `deploy-modal.bat` remains available independently. The frontend only shows real ComfyUI URLs discovered from deployment logs, not guessed subdomains.

Cloud output uses `storage/hf_sync.py` (Modal Volume to HF archive and R2 previews).
Build and deployment remain in `comfyui.py`. The web UI is not baked into
the Modal image.

## Contributing

Please feel free to contribute to make this project better.
Performance improvements/optimizations are very welcome.

## Separate Modal Secret setup

参见上文 **Windows / Linux 命令行操作速查 → 1. 上传 / 初始化 Modal Secrets**，分别运行 setup-secrets.bat 或 bash setup-secrets.sh。

## 致谢 / Acknowledgements

特别感谢 [caru-ini](https://github.com/caru-ini) 和原项目 [modal-comfyui](https://github.com/caru-ini/modal-comfyui) 的贡献。本 Fork 以其开源实现为基础，持续增加工作流部署、资产存储与可视化管理功能；原项目的设计与贡献归属于原作者及其贡献者。


## 多 Endpoint Task Runner

统一入口为 `batch_run.py --task`，支持跨 Modal 账号的 Qwen 图片生成与 H3 视频生成：

```powershell
uv run python batch_run.py --task tasks/005_last_train.json --endpoints endpoints.json
```

- `endpoints.json` 为每个 ComfyUI 实际地址定义别名。不同别名可以属于不同 Modal 账号。
- 每个 Task 定义 `name`、`mode` 和有序 `steps`。每个 step 使用 `endpoint`、`workflow`（API JSON）、`set` 和可选的 `foreach`。
- `"mode": "stream"`（默认）表示上游单个 job 完成后立即触发对应下游；`"mode": "batch"` 表示等引用的整个上游 step 完成后才触发视频任务。
- `$item.prompt` 引用当前镜头字段；`$scenes.01.image` 引用上游图片；`@path/to/image.png` 使用本地图片。Runner 自动下载和跨 Endpoint 上传。
- 状态保存在 `outputs/<task>/<step>/<job>/status.json`，包含 `prompt_id` 和 `intent_hash`。恢复前校验提示词、Workflow、Endpoint、上游 job 和本地输入文件内容，发生变更时**拒绝复用旧结果**，不会自动重复付费提交；应使用新 Task 名或 `--out` 目录重新运行。
- 旧版 `status.json` 没有 `intent_hash` 时，默认拒绝恢复。确认旧结果与当前任务匹配后，可显式加 `--trust-legacy-status` 采纳旧状态（仅为旧状态补充指纹，不重新生成）。提交响应不确定的任务仍必须先人工核查远程队列。
- 两种模式均由一个轻量调度器运行，并使用本地状态文件和任务锁；不依赖 Celery、Redis 或跨账号共享 Volume。

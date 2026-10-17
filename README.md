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

Each `workflows/<name>/` folder contains exactly one **ComfyUI API graph** plus `models.json` and `plugins.json`. The graph can have any name. Do not put extra JSON examples in this folder because the loader detects the graph by excluding only `models.json`, `model.json`, and `plugins.json`.

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
  "gpu": "L40S",
  "cuda_devel": true
}
```

- `models`: Hugging Face Hub models. Each requires `repo_id`, `filename`, and `model_dir`; `local_filename` optionally renames the linked file.
- `models_ext`: external direct downloads. Each requires `url`, `filename`, and `model_dir`; use `[]` if none.
- `model_dir`: relative to `/root/comfy/ComfyUI/models/`, or an absolute destination.
- `gpu`: Modal GPU specification; `MODAL_GPU` overrides it. Current workflows target `L40S`.
- `cuda_devel`: optional boolean, enabling a CUDA 12.6 development image for compiling custom extensions.

#### `plugins.json`

```json
{
  "comfy_plugins": [],
  "comfy_plugins_ext": [
    {
      "url": "https://github.com/visualbruno/ComfyUI-Hunyuan3d-2-1.git",
      "requirements": ["requirements.txt"],
      "build_commands": [
        "cd hy3dpaint/custom_rasterizer && CC=gcc CXX=g++ TORCH_CUDA_ARCH_LIST=8.9 pip install --no-build-isolation ."
      ]
    }
  ]
}
```

- `comfy_plugins`: optional list of node names/IDs for `comfy node install`.
- `comfy_plugins_ext`: optional list of Git custom nodes. `url` is required; `branch`, `requirements`, `install` (Python script), `ext_deps`, and `build_commands` are optional.
- `requirements` and `ext_deps` are installed using `--no-deps`; `build_commands` runs as shell commands in the cloned plugin directory. Only configure trusted commands.
- For no extra plugins use `{"comfy_plugins": [], "comfy_plugins_ext": []}`. The deployment still runs `comfy node install-deps` on the API graph.

For complete real examples see `workflows/comfyui-hunyuan3d-2-1/`. ComfyUI Manager → **Used in Workflow** can help identify missing nodes.

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

## Features

- **Auto-scaling**: Scales down to zero when not in use to save costs.
- **GPU Snapshots**: Fast startup times using Modal's GPU snapshots.
- **Model Caching**: Uses Modal Volumes to cache models across runs.
- **Custom Node Management**: Integrated with `comfy-cli` for easy plugin installation.

## Cloudflare R2 GLB storage

GLB files saved under `ComfyUI/output/3d/` are written directly to Cloudflare R2
through `modal.CloudBucketMount` (no background upload process).

- Bucket: `modal-studio-assets` (verified in Cloudflare).
- Object key: `comfyui/<workflow>/3d/<filename>.glb`.
- In **Modal Dashboard → Secrets**, create `cloudflare-r2` containing
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` from an R2 Object Read & Write
  API token scoped to this bucket. No access keys belong in this Git repository.
- The R2 endpoint defaults to the configured Cloudflare account; override via
  the local `R2_ENDPOINT` environment variable before deployment if necessary.
- Deploy with `./deploy-modal.bat <workflow>` on Windows.
- Download persisted GLB files using R2 Dashboard, a configured public custom
  domain, or presigned URLs; ComfyUI's built-in download still uses its UI.

Only `output/3d/` is persisted to R2; other ComfyUI outputs remain ephemeral.
File writes need to complete normally. Modal/S3 mounts do not support all POSIX
filesystem operations, and a real GLB write must be verified after deployment.

## Local console

Run `uv run python frontend/server.py` and open http://127.0.0.1:8768 .
The console manages Modal workflows and previews private R2 / Hugging Face S3
assets. It binds only to localhost and reads local credentials from `.env`.
The original `deploy-modal.bat` remains available independently.

Cloud output uses `storage/hf_sync.py` (R2 to HF replication).
Build and deployment remain in `comfyui.py`. The web UI is not baked into
the Modal image.

## Contributing

Please feel free to contribute to make this project better.
Performance improvements/optimizations are very welcome.

## Separate Modal Secret setup

Keep all three credentials in one local `.env` file (ignored by Git). Configure
Secrets explicitly, **not** during deployment:

```powershell
Copy-Item .env.example .env
# Fill the R2, HF and GitHub credentials in .env
.\setup-secrets.bat
```

`setup-secrets.bat` creates missing Modal Secrets: `cloudflare-r2` (required),
`huggingface-secret` and `github` (optional). Existing Secrets are reused,
never overwritten. To rotate an existing Secret, update it in Modal Dashboard.

Deploy separately:

```powershell
.\deploy-modal.bat community-pixal3d-trellis2-tuning
```

Deployment does not read `.env` or change any Secret.

## Preview private R2 GLB files

The unified asset browser is available via `uv run python frontend/server.py` at http://127.0.0.1:8768.
Legacy `preview_r2.py` and `preview_hf.py` remain optional standalone previews for now.



## 致谢 / Acknowledgements

特别感谢 [caru-ini](https://github.com/caru-ini) 和原项目 [modal-comfyui](https://github.com/caru-ini/modal-comfyui) 的贡献。本 Fork 以其开源实现为基础，持续增加工作流部署、资产存储与可视化管理功能；原项目的设计与贡献归属于原作者及其贡献者。

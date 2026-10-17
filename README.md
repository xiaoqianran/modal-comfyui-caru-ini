# modal-comfyui

Run ComfyUI on Modal with auto-scaling, GPU snapshots, and easy model management.

Good for testing wan2.2 or other video generation models.

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

To deploy ComfyUI as a persistent app:
```bash
modal deploy comfyui.py
```

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

Run `uv run python preview_r2.py` and open http://127.0.0.1:8766 .
It streams GLB files from the private R2 bucket through a local-only server, without changing ComfyUI or enabling public bucket access.



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

### Models

Copy `models.example.py` to `models.py` and edit it to manage your models. You can specify:
- Hugging Face models(`models`) using `repo_id` and `filename`.
- External models(`models_ext`, e.g. civitai) using a direct `url`.

Models are downloaded to volumes and symlinked to the specified `model_dir`.

`model_dir` accepts two styles:
- **Relative path** (recommended for standard ComfyUI folders): resolved under `/root/comfy/ComfyUI/models/`. e.g. `"checkpoints"` → `/root/comfy/ComfyUI/models/checkpoints`.
- **Absolute path**: used as-is. Use this when the target lives outside `ComfyUI/models/` (e.g. a custom node's own model directory).

See `models.example.py` for reference.

### Plugins and Custom Nodes

Copy `plugins.example.py` to `plugins.py` and edit it to add custom node IDs or titles to be installed via `comfy-cli`.
- **Workflow Dependencies**: If you have a `workflow_api.json` in the root directory, the setup will automatically install the necessary custom nodes for that workflow.

### In case of Insufficient Custom Node

Open ComfyUI manager on comfyui and click "Used in Workflow" to see which custom nodes are used in the workflow.

Add these custom nodes to `plugins.py`(be careful of node id).

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



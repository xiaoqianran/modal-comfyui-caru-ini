# B300 Image + Video Suite

One Modal ComfyUI instance contains Qwen Image 2.1, MiniMax H3 FL2VA BF16, MiniMax H3 REF2VA BF16, and LTX-2.5 Official BF16 workflows.

## Deploy

PowerShell from repository root:

```powershell
$env:WORKFLOW = "comfyui-b300-image-video-suite"
uv run modal deploy comfyui.py
```

The models.json contains **21 deduplicated files**. All workflow JSONs are mounted under `user/default/workflows/comfyui-b300-image-video-suite/<source-name>/` so they remain separately selectable.

## Residency and charges

- This deployment uses `gpu="B300"`, `min_containers=1` and the ComfyUI `--highvram` option.
- All models are downloaded to persistent storage during image construction; this does **not** preload them into VRAM.
- Load each model once by running its workflow. `--highvram` tries to retain previously loaded weights in VRAM when capacity allows. ComfyUI may still evict weights under memory pressure; this is **not a strict all-weights-pinned implementation**.
- A minimum container incurs charges continuously even when the UI is idle. Stop the deployed app when no longer needed.
- True preloading and full simultaneous residency require workload-specific startup warmups, instrumentation of loaded models, and tests on an actual B300. GPU snapshots alone do not prove residency.
- The existing volume and outputs are shared with other workflows; do not delete these volumes while using other deployments.

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

- This deployment uses `gpu="B300"` and ComfyUI `--highvram`. `min_containers=0` by default, to prevent `modal run comfyui.py::download_models` from automatically starting a GPU container. Set `MODAL_PREWARM=1` **only when deploying after the model download finishes** to keep one GPU container warm.
- Models are initialized separately via `modal run comfyui.py::download_models` and stored in a persistent Volume. Image builds do not download model weights. This does **not** preload them into VRAM.
- Load each model once by running its workflow. `--highvram` tries to retain previously loaded weights in VRAM when capacity allows. ComfyUI may still evict weights under memory pressure; this is **not a strict all-weights-pinned implementation**.
- A minimum container incurs charges continuously even when the UI is idle. Stop the deployed app when no longer needed.
- True preloading and full simultaneous residency require workload-specific startup warmups, instrumentation of loaded models, and tests on an actual B300. GPU snapshots alone do not prove residency.
- The existing volume and outputs are shared with other workflows; do not delete these volumes while using other deployments.
# 独立模型缓存初始化（Linux）

模型权重保存在 `modal-comfyui-models-v2` Volume 中，**不在镜像构建阶段下载**。
首次使用某个 Modal 账号 / Environment，或者更新 `models.json` 后执行：

```bash
export WORKFLOW=comfyui-b300-image-video-suite
export MODAL_GPU=B300
uv run modal run comfyui.py::download_models
uv run modal deploy comfyui.py
```

后续仅修改插件或 ComfyUI 代码时，直接运行 `uv run modal deploy comfyui.py`，无需重新下载模型。
下载任务会打印 `CACHE HIT` / `DOWNLOADED`；ComfyUI 启动时仅从 Volume 离线建立模型链接。
如果 Volume 缺少文件，服务启动会明确提示先执行 `download_models`，不会自动在 GPU 容器上下载。

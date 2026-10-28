# Community MiniMax H3 Studio

One Modal ComfyUI environment that combines both BF16 MiniMax H3 model
families plus the MiniMaxH3-Studio custom node, so a single deployment can
run all of the workflows below. Merged from
`001-minimax-h3-fl2va-bf16` and `002-minimax-h3-ref2va-bf16`.

## Workflows

| File | Mode | Checkpoint |
| --- | --- | --- |
| `video_minimax_h3_t2v.json` | Text to video | FL2VA BF16 |
| `video_minimax_h3_i2v.json` | Image to video | FL2VA BF16 |
| `video_minimax_h3_r2v.json` | Reference to video | Ref2VA BF16 |
| `video_minimax_h3_multiframe_reference.json` | Multiframe reference | Ref2VA BF16 |
| `video_minimax_h3_fun_controlnet_union.json` | Fun ControlNet (uses the SDPose models below) | Ref2VA BF16 |

Workflow files are copied verbatim from the two source folders, in their
original formats.

## Models

`models.json` is the union of the two source `models.json` files
(5 + 8 entries, 3 shared, 10 unique): both BF16 diffusion checkpoints
(FL2VA + Ref2VA), the shared Qwen3-VL text encoder and video/audio VAEs,
both Turbo LoRAs, the Fun ControlNet model patch, and the two SDPose
models used by the ControlNet workflow. GPU: `RTX-PRO-6000`.

Merging only increases the model download/cache size (both checkpoints
live in the shared model Volume). Each run still loads a single
checkpoint; switching between FL2VA and Ref2VA workflows in one session
makes ComfyUI swap the loaded diffusion model.

## Plugin

MiniMaxH3-Studio is a ComfyUI **custom node** (H3 Context nodes +
Studio sidebar), not a workflow. It is installed through `plugins.json`:

```json
{
  "comfy_plugins": [],
  "comfy_plugins_ext": [
    {
      "url": "https://github.com/rookiestar28/MiniMaxH3-Studio",
      "commit": "e3389d4e76dd276eeea965b14ceca78626d59bea"
    }
  ]
}
```

The commit pin is the Studio 1.4.0 HEAD at the time of adding; remove or
update `commit` to track upstream. The plugin has no Python dependencies,
does not download models, and its frontend is prebuilt, so no
`requirements` or `build_commands` are needed. It requires ComfyUI's
native MiniMax H3 nodes and official `video_minimax_h3_*` templates,
which this deployment already uses.

## Known limitation on Modal (Linux)

Studio's prompt pipeline, canvas nodes and sidebar work on Linux. Its
clip editor, Production import, longer-video assembly and final
rendering currently require Windows x64 plus a specific FFmpeg build
(upstream: Linux support is coming), so those media features are not
expected to fully work in this cloud deployment.

## Deploy

```powershell
.\deploy-modal.bat community-minimaxh3-studio
```

or

```bash
WORKFLOW=community-minimaxh3-studio uv run modal deploy comfyui.py
```

## Faster first download (dynamic resources)

The 10 merged models total roughly 164 GB (the two BF16 diffusion
checkpoints are ~66 GB each), so the first build's download step is the
slow part. `comfyui.py` downloads files in parallel threads inside a
single build container, and that container's resources are adjustable
at deploy time through environment variables — no code edits needed.
They only affect the build-time download container, not the GPU
runtime container:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODEL_DOWNLOAD_CPU` | `16` | CPU cores requested for the download container (0.25–128) |
| `MODEL_DOWNLOAD_MEMORY` | `32768` | Memory request in MiB |
| `MODEL_DOWNLOAD_WORKERS` | `0` = adaptive | Parallel file threads (1–32); `0` means `min(file count, CPU cores)` |
| `MODEL_DOWNLOAD_TIMEOUT` | `7200` | Download build-step timeout in seconds |

The defaults are already the fast tier (CPU 16 / memory 32768 MiB), so a
plain deploy runs at that speed with no setup. To push even higher, for
example:

```powershell
$env:MODEL_DOWNLOAD_CPU="32"; $env:MODEL_DOWNLOAD_MEMORY="65536"; $env:MODEL_DOWNLOAD_WORKERS="10"
.\deploy-modal.bat community-minimaxh3-studio
```

The build log prints the effective settings
(`Downloading 10 model files: workers=..., cpu=..., memory=... MiB`)
and the total download time when it finishes. Note that more threads
is not always faster: the two ~66 GB checkpoints dominate, and a
single file is already split into parallel chunks by Hugging Face
Xet, so bandwidth and Volume write speed cap the total. Model files are
cached in the Volumes v2 volume `modal-comfyui-models-v2`.

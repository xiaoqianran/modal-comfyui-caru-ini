# Qwen-Image-2.1 / Modal RTX PRO 6000 Benchmark

Date: 2026-10-24. GPU: NVIDIA RTX PRO 6000 Blackwell Server Edition (96 GB).
ComfyUI 0.42.0; PyTorch 2.14.1+cu130. Model: Qwen-Image-2.1 BF16 diffusion + Qwen3-VL BF16 encoder; Qwen3.5 PE T2I INT8.

## Method

- Official T2I model nodes, 1 image each, fixed prompt and seed 20261025.
- KSampler Euler, scheduler simple, CFG 1.0, batch size 1.
- Warm-up separately used seed 20261026 and was excluded.
- Each measured case generated a new image. The repeated unchanged model/conditioning nodes may remain cached by ComfyUI; sampling was not cache-hit.
- Wall time from successful POST /prompt until completed /history/{id}. Includes sampling, decode, save, network polls; excludes cold start and first model load.
- VRAM calculated via `(vram_total-vram_free)/2**30` from /system_stats sampled periodically; **sampled maximum**, not precise torch/CUDA peak.
- Each case is one measured run, not an average. Idle VRAM after warming ≈28.84 GiB.
- Generated image verified from ComfyUI history outputs (one per case).

| Test | Resolution | Steps | Wall seconds | Sampled peak VRAM (GiB) |
| --- | --- | ---: | ---: | ---: |
| A | 1024 x 1024 | 20 | 6.13 | 29.27 |
| B | 1024 x 1024 | 25 | 6.23 | 29.27 |
| C | 1024 x 1024 | 35 | 7.97 | 29.27 |
| D | 1536 x 1536 | 25 | 14.43 | 29.76 |
| E | 2048 x 2048 | 25 | 29.94 | 30.45 |

## Reproduction

```powershell
.\deploy-modal.bat comfyui-qwen-image-2-1
.\.venv\Scripts\python.exe -u testing/qwen21_benchmark.py
```

Deployment URL: https://xiaoshuhuaer--modal-comfyui-qwen-image-2-1-comfyui-ui.us-east.modal.run

Initial deployment problems resolved: Modal GPU string must be `RTX-PRO-6000`; `os.memfd_create` absent from this Python runtime was repaired by a build-time ComfyUI-only patch. Do not override PYTHONPATH for Modal workers.

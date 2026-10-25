# FastH3 Omni BF16 — official ComfyUI workflow

Official template: https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_fasth3_ominiref_r2v.json
Adapted from official INT8 template to FastH3 Omni BF16 diffusion weights and FP16 video VAE.
8 steps; native MiniMaxH3SigmaShift video=12 audio=3.
The FL2VA Turbo LoRA and FL2VA diffusion model belong to a different workflow and are intentionally not included.

## Experimental Omni BF16 conditioning modes (2026-10-25)

All three files are UI workflows mounted into ONE Modal deployment, with the same `fasth3_omniref_8step_pruned_bf16.safetensors` checkpoint, 8 steps and sigma shifts 12/3:

- `video_fastvideo_fasth3_omni_r2v.json`: official-derived reference conditioning; existing workflow.
- `video_fastvideo_fasth3_omni_t2v.json`: native `MiniMaxH3ImageToVideo` with no keyframe inputs.
- `video_fastvideo_fasth3_omni_flf2v.json`: native `MiniMaxH3ImageToVideo` with separate first/last keyframe images.

CAUTION: Although ComfyUI exposes these native T2V/FLF2V conditioning inputs, FastH3 OmniRef is primarily distilled for reference-to-video. Structural validation alone does not establish that the OmniRef checkpoint is behaviorally compatible with these other conditioning sequences. These two experimental workflows have NOT been GPU-tested. Do not treat them as production-ready before testing both first/last anchoring and video/audio decode on a deployed GPU. ComfyUI UI workflows require API-format conversion before submitting via `/prompt`; the existing Modal runtime only mounts the files and does not offer per-request workflow selection automatically.

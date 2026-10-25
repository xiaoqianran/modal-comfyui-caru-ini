# FastH3 Omni BF16 — official ComfyUI workflow

Official template: https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_fasth3_ominiref_r2v.json
Adapted from official INT8 template to FastH3 Omni BF16 diffusion weights and FP16 video VAE.
8 steps; native MiniMaxH3SigmaShift video=12 audio=3.
The FL2VA Turbo LoRA and FL2VA diffusion model belong to a different workflow and are intentionally not included.

## Additional local modes (2026-10-25)

All three UI workflows are mounted into the same Modal ComfyUI instance under this directory:

- `video_fastvideo_fasth3_omni_r2v.json`: existing OmniRef reference-to-video graph.
- `video_fastvideo_fasth3_omni_t2v.json`: local adaptation using native `MiniMaxH3ImageToVideo`, no frame inputs.
- `video_fastvideo_fasth3_omni_flf2v.json`: local adaptation using native `MiniMaxH3ImageToVideo` with separate `first_frame` and `last_frame` LoadImage inputs.

R2V uses the FastH3 OmniRef BF16 8-step checkpoint. T2V and FLF2V use the official MiniMax H3 FL2VA BF16 checkpoint at 20 steps, because the OmniRef distilled weights target reference-to-video. Both checkpoints are downloaded into and served from ONE Modal ComfyUI deployment, not separate deployments. Actual hosted load/execution and output quality are NOT yet verified. Do not confuse the native local conditioning node with the hosted MiniMax partner API node. Upload the two named input images in ComfyUI before executing FLF2V. The files here are ComfyUI UI workflows, not API-format `/prompt` payloads; API clients must serialize them into API graphs before submission. A redeploy is needed for new mounted workflow files to appear in the hosted ComfyUI instance.

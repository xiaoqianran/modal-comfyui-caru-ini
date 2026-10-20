# LTX-2.5 · ComfyUI official templates

Source: https://github.com/Comfy-Org/workflow_templates/tree/main/templates

- `video_ltx2_5_t2v.json` — official T2V
- `video_ltx2_5_i2v.json` — official I2V
- `video_ltx2_5_flf2v.json` — official first/last frame

All three JSON files are unmodified upstream templates.

Main transformer and Gemma 12B encoder are **INT8 ConvRot**, not BF16; VAEs and x2 latent upscaler are **BF16**. The Gemma E2B enhancer is optional; the default prompt enhancer is disabled. No third-party ComfyUI node package is declared, but the bundled ComfyUI version must include the LTX-2.5 core nodes. GPU inference has not been tested.

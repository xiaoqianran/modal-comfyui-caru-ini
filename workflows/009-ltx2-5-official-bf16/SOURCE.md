# LTX-2.5 official BF16 variants

Based on the three Comfy-Org templates in `../009-ltx2-5-official`; only model filenames replaced in workflow JSON files.

- LTX-2.5 distilled 22B BF16 main model and Gemma4 12B BF16 text encoder: `Lightricks/LTX-2.5`.
- Gemma4 E2B BF16 optional prompt enhancer: `Comfy-Org/gemma-4` (disabled by default).
- BF16 audio/video VAE and spatial upscaler are shared with original templates.
- No third-party plugins declared. Native LTX-2.5 ComfyUI nodes required.
- Model download, loading, and GPU inference not yet tested.

Source: https://github.com/Comfy-Org/workflow_templates/tree/main/templates

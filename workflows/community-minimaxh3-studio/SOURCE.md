# Sources

## Model workflows
- FL2VA (BF16): T2V and I2V workflows (`video_minimax_h3_t2v.json`,
  `video_minimax_h3_i2v.json`) from Comfy-Org/workflow_templates, copied
  verbatim from `workflows/001-minimax-h3-fl2va-bf16/`.
- Ref2VA (BF16): R2V, multiframe reference and Fun ControlNet workflows
  (`video_minimax_h3_r2v.json`, `video_minimax_h3_multiframe_reference.json`,
  `video_minimax_h3_fun_controlnet_union.json`) from
  Comfy-Org/workflow_templates, copied verbatim from
  `workflows/002-minimax-h3-ref2va-bf16/`.

## Custom node
- MiniMaxH3-Studio (ComfyUI custom node / sidebar) by rookiestar28,
  https://github.com/rookiestar28/MiniMaxH3-Studio, installed via
  `plugins.json` (`comfy_plugins_ext`, commit-pinned).

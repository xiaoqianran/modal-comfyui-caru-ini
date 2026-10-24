# PixelArtistry Pixal3D Multiview — Modal 运行记录

## 2026-10-17 最近一次运行

- 应用：`modal-community-pixal3d-multiview-pixelartistry`
- Modal App ID：`ap-c4eYohWZAF3zdSukfCRF2Y`
- 日志时间区域：`UTC+08:00`
- 应用创建：`2026-10-17 19:06:58`
- 应用停止：`2026-10-17 19:26:33`
- **应用存续时间：19 分 35 秒**（不等于推理耗时）
- 首次 `App Ready!`：`19:08:14`
- 第二次 `App Ready!`：`19:14:51`（与其间再次构建 / 启动日志对应，不能据此单独认定为推理启动）
- **单次完整 ComfyUI 工作流推理耗时：未从目前 Modal 网关日志确认**；需要 ComfyUI 执行历史、`Prompt executed in ...` 或任务开始/结束时间戳才能精确计算。

### 模型下载 / 缓存解析

日志：`2026-10-17 19:09:44` 开始 `download_all`；`19:10:17` 最后一个模型显示 `Downloaded`。**耗时约 33 秒**。此处 `Downloaded` 表示下载函数完成并在 ComfyUI 模型目录建立链接；部分条目命中 Hugging Face / Modal Volume 缓存，不应解释为十个模型全部从头传输。

| 时间 (UTC+08) | 模型 |
|---|---|
| 19:09:47 | `qwen_image_2.1_int8_convrot.safetensors` |
| 19:09:47 | `pixal3d_multiview_int8_convrot.safetensors` |
| 19:09:47 | `qwen3vl_8b_int8_convrot.safetensors` |
| 19:09:47 | `qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors` |
| 19:10:16 | `qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors` |
| 19:10:16 | `qwen_image_2.1_vae_bf16.safetensors` |
| 19:10:16 | `trellis_2_shape_vae_bf16.safetensors` |
| 19:10:16 | `trellis_2_texture_vae_bf16.safetensors` |
| 19:10:16 | `dino_v3_L_naf_fp32.safetensors` |
| 19:10:17 | `birefnet.safetensors` |

### 已验证的资产结果

- `2026-10-17 19:21:43`：Hugging Face 同步日志报告上传成功。
- 文件：`ComfyUI_00001.glb`
- 文件大小：`63,582,580 bytes`（约 `60.64 MiB`）
- 目标：`hf://comfyui-assets/comfyui/community-pixal3d-multiview-pixelartistry/3d/ComfyUI_00001.glb`
- 说明：这里只能确认该文件在上述时刻上传成功，**不能**将上传时间当作工作流推理结束时间或推算精确推理时长。

### 排查与复现

```powershell
uv run modal app logs ap-c4eYohWZAF3zdSukfCRF2Y --timestamps --tail 3000
uv run modal app logs ap-c4eYohWZAF3zdSukfCRF2Y --timestamps --tail 3000 --search "Downloaded"
uv run modal app logs ap-c4eYohWZAF3zdSukfCRF2Y --timestamps --tail 3000 --search "Uploaded"
```

> 本记录根据 Modal CLI 提供的应用列表与日志写成。时间为日志事件时间，推理阶段缺乏完整可验证的起止标记。未据此判断 GPU 实际占用时间、成本或性能。

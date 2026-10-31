# B200+ MiniMax H3 INT8 ConvRot / Pika H3 Flex

独立套件 `010-minimax-h3-b200plus-suite`，与 `010-minimax-h3-bf16-b200plus-suite` 分开部署，不修改原 BF16 工作流。

- GPU 默认 `B200+`，单 GPU 容器、单 ComfyUI 进程；`highvram=false`。
- 基础视频 DiT：`minimax_h3_fl2va_int8_convrot.safetensors` 与 `minimax_h3_ref2va_int8_convrot.safetensors`（INT8 ConvRot）。
- 视频 VAE：`minimax_h3_video_vae_int8_convrot.safetensors`；音频 VAE：FP32。
- Qwen3VL 文本编码器：NVFP4 AWQ。**Pika H3 Flex LoRA 仍为 BF16**，并非 INT8；使用官方对应的 FL2VA、Ref2VA 适配器。
- 三套 UI 工作流：T2V、FL2VA、Ref2VA；三者使用 INT8 基础权重。
- 使用项目公共模型 Volume，首次部署前需下载缺失的 INT8 权重。无须重复下载已有的 BF16 LoRA、音频 VAE 或编码器。

## Linux 部署（亚太大区域）

```bash
git pull
export WORKFLOW=010-minimax-h3-b200plus-suite
export MODAL_GPU="B200+"
export MODAL_REGION=ap
uv run modal run download-models.py
uv run modal deploy comfyui.py
```

如果 INT8 权重已经存在于共享 Volume，可跳过下载命令。Windows 使用对应的 `$env:WORKFLOW`、`$env:MODAL_GPU`、`$env:MODAL_REGION` 环境变量即可。

三个 UI 工作流位于 `001-minimax-h3-fl2va-int8/` 与 `002-minimax-h3-ref2va-int8/` 下。

**限制**：权重文件和工作流的静态引用已核对，但未经实际 GPU 推理验证；Pika BF16 LoRA 在 INT8 ConvRot 底座上的效果、显存占用和生成质量需要实测，不保证与 BF16 套件完全相同。
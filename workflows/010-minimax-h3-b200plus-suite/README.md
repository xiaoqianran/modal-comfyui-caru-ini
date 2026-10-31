# B200+ Pika H3 专用套件

保留原部署标识 `010-minimax-h3-b200plus-suite`，避免改变已有 Modal 应用名称及入口。
仅提供 Pika H3 Flex FL2VA / Ref2VA 两个 UI 工作流；不再包含原生 MiniMax H3 工作流。

- GPU：默认 B200+，单个 GPU 容器、单个 ComfyUI 进程。
- `highvram=false`：此套件不传 `--highvram`，由 ComfyUI 管理 FL2VA / Ref2VA 模型切换，降低连续推理时显存溢出风险；其他工作流保持原设置。
- 7 个模型：两套 H3 BF16 基础权重、两套 Pika LoRA、视频/音频 VAE 和 Qwen3VL 编码器。
- 模型继续复用公共 Volume；从模型清单移除某文件**不会删除**公共缓存文件，也不会影响其他工作流。
- 本改动不能保证绝不 OOM，需部署后测试连续 FL2VA → Ref2VA。

## 部署

PowerShell：

```powershell
$env:WORKFLOW = "010-minimax-h3-b200plus-suite"
Remove-Item Env:MODAL_GPU -ErrorAction SilentlyContinue
uv run modal deploy comfyui.py
```

Linux：

```bash
export WORKFLOW=010-minimax-h3-b200plus-suite
unset MODAL_GPU
uv run modal deploy comfyui.py
```

模型已存在于公共 Volume 时无须重新下载；若缺失，先运行 `uv run modal run download-models.py`。
前端在该套件目录选择 `video_pika_h3_flex_fl2va.json` 或 `video_pika_h3_flex_ref2va.json`。

# B200+ MiniMax H3 双模型聚合

单个 Modal 应用、单个 GPU 容器和单个 ComfyUI 进程，同时提供 FL2VA、Ref2VA 及对应 Pika H3 Flex 工作流。原始 001 / 002 工作流不变。

模型清单 `models.json` 从原始 001 / 002 BF16 配置合并并按 Hugging Face repo/filename 去重，共 12 个模型文件；包含两种 H3 diffusion 权重、共用 VAE 和文本编码器、两套 Turbo LoRA、两套 Pika LoRA，以及 Ref2VA 辅助权重。

## 下载与部署

PowerShell（仓库根目录）：

```powershell
$env:WORKFLOW = "010-minimax-h3-b200plus-suite"
$env:MODAL_GPU = "B200+"
uv run modal run download-models.py
uv run modal deploy comfyui.py
```

Linux：

```bash
export WORKFLOW=010-minimax-h3-b200plus-suite
export MODAL_GPU=B200+
uv run modal run download-models.py
uv run modal deploy comfyui.py
```

ComfyUI 左侧 Workflow 浏览器中，选择 `010-minimax-h3-b200plus-suite/001-minimax-h3-fl2va-bf16` 或 `010-minimax-h3-b200plus-suite/002-minimax-h3-ref2va-bf16` 子目录的工作流。包括 Pika 两种对应工作流。

## 常驻与注意事项

- `gpu=B200+`、自动 `--highvram`、`min_containers=1`、`max_containers=1`，只启动一个 ComfyUI 进程；共用现有模型 Volume。
- 模型下载在独立 CPU Modal 任务运行，不会在 GPU 容器内下载；已缓存的文件由下载器复用。
- 工作流按需切换模型；`--highvram` **不保证两套 BF16 diffusion 权重始终同时驻留显存**，ComfyUI 可能按显存压力卸载。
- 部署后需在实际服务中检查模型加载及生成成功，配置验证不等同于远程生成验证。

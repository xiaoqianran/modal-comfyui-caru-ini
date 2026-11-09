# 012 RelayVSR X2 视频超分（ComfyUI 原生）

## 来源
- ComfyUI 官方原生支持 PR：https://github.com/Comfy-Org/ComfyUI/pull/17511 （2026-11-05 合并）。
- 工作流来自该 PR 附件：https://github.com/user-attachments/files/34450607/relayvsr_x2_video.json
- 模型权重：https://huggingface.co/Comfy-Org/RelayVSR

## 工作流及模型
- 本目录的 relayvsr_x2_video.json 从官方 PR 示例获取，仅将 UNETLoader 的旧文件名
  RelayVSR\\relayvsr_x2_int8_convrot.safetensors 调整为 Comfy-Org 现有发布的
  relayvsr_x2_lightx2v_int8_convrot.safetensors；其余采样及节点连接保持官方配置。
- 执行 2 倍视频超分；默认 1 step / cfg 1；使用 RelayVSRConditioning、VSRDecode。
- 两份权重由 models.json 下载到 Modal Volume V2，并链接至 ComfyUI/models/diffusion_models 与 vae。
- plugins.json 不安装第三方节点，要求 ComfyUI 版本包含 PR #17511（master 于 2026-11-05 合并）。
- 示例工作流默认输入 0036.mp4，运行前在 LoadVideo 节点替换成实际输入。
- 输出文件位于 ComfyUI 输出目录，完整视频耗时与显存尚未在 Modal GPU 上实测。

## 部署
```powershell
$env:WORKFLOW='012-video-upscale-relayvsr'
$env:PYTHONIOENCODING='utf-8'
uv run modal deploy comfyui.py
```

说明：deploy 前现有代码会通过 CPU-only MODEL PREFLIGHT 检查并补齐缓存，可能下载大权重。
首次运行建议使用 3~5 秒的短片；不要假定最新 ComfyUI Stable 已包含该新节点。
本次仅新增配置和工作流，不自动触发模型下载、GPU 推理或远程部署。

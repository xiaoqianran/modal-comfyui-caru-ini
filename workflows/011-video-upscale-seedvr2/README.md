# 011 SeedVR2 7B FP16 — ComfyUI 官方模板

## 来源
- 官方模板：https://github.com/Comfy-Org/workflow_templates/blob/main/templates/utility_seedvr2_video_upscale.json
- 对应自定义节点：https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler （官方模板所需的旧版节点接口）
- 本目录 `utility_seedvr2_video_upscale.json` 是官方模板原样下载，不再保留旧作者的 HD 示例模板。

## 配置
- GPU RTX PRO 6000，7B FP16，无量化。
- 官方模板内 DiT `cuda:0`、BlockSwap 0、offload `none`；VAE `cuda:0`。
- 已移除 ImageFromBatch 的 96 帧截断；全部输入帧直接进入 SeedVR2。长视频需注意显存。
- 请关闭已打开的旧工作流标签，从官方 JSON 重新加载。已有运行队列提交的旧 Graph 不会自动更新。

## 部署
```powershell
$env:WORKFLOW='011-video-upscale-seedvr2'
$env:PYTHONIOENCODING='utf-8'
uv run modal deploy comfyui.py
```
部署前自动验证缓存；首次测试建议使用 5 秒 H3 视频。

## 兼容性说明
Comfy-Org 官方工作流仍依赖传统 SeedVR2LoadDiTModel / SeedVR2LoadVAEModel 节点；comfyorg/comfyui_seedvr2 当前仓库只注册新接口 SeedVR2 等节点，直接混用会显示缺失节点。这里保留官方工作流 JSON，使用兼容节点插件，不恢复旧作者的示例工作流。

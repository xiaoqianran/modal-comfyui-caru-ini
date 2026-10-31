# YuE2-3B（ComfyUI 官方工作流）

- 模型：<https://huggingface.co/Comfy-Org/YuE2>（CC BY-NC 4.0，非商业许可）
- 工作流：<https://github.com/Comfy-Org/workflow_templates/tree/main/templates>
- `audio_yue2_text2music.json`：官方文本生成音乐模板。
- `audio_yue2_music_cover.json`：官方音乐翻唱模板，需要输入音频。
- GPU：L40S；默认单实例；沿用项目的模型下载检查、Volume 与部署逻辑。
- 实际部署前确认当前 ComfyUI 版本包含 YuE2 原生节点。

模型下载与部署使用根目录现有脚本，勿在容器内重复下载。

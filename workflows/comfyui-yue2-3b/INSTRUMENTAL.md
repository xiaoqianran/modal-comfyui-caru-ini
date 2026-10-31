# YuE2 Instrumental / 纯音乐

`audio_yue2_instrumental.json` 为 ComfyUI 可视化工作流，`audio_yue2_instrumental_api.json` 为 API / Runner 格式。原来的 `audio_yue2_text2music.json` 仍用于带人声的歌曲。

流程：`YuE2GenerateABC`（曲式标签，不需要歌词正文）→ `YuE2InstrumentalizeABC`（官方 Skill 的 Vocal → Ins 音符迁移与校验）→ `YuE2GenerateMusic`（Lyrics 为空）→ KSampler → VAE → FLAC。

自定义节点位于项目根目录 `vendor_nodes/yue2_instrumental/`。它的转谱代码来源于已安装的 YuE2 官方 Instrumental Skill，保留其 MIT 许可证；只在 `WORKFLOW=comfyui-yue2-3b` 的 Modal 镜像中安装，不影响其它工作流。

第一次使用须重新部署：

```powershell
$env:WORKFLOW="comfyui-yue2-3b"
$env:MODAL_GPU="L40S"
uv run modal deploy comfyui.py
```

然后在 ComfyUI 的工作流列表打开 `comfyui-yue2-3b/audio_yue2_instrumental.json`。Style 描述目标器乐与配器；ABC 规划的 Lyrics 仅使用 `[Intro]`、`[Verse]` 等段落标签；音乐生成节点的 Lyrics 保持空白。不要使用原版歌曲模板替代纯音乐模板。

验收标准：加载后节点 `YuE2 Instrumental: Vocal → Ins` 无 Missing Node，生成检查结果 `vocal_notes_after = 0`，完成后试听确认无歌声。曲谱无 Vocal 音符不意味着音频百分之百无拟人声，仍需听觉检查。

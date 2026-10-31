# 《未说出口的温柔》：三种钢琴演绎对照

本任务从同一份 YuE2 原创 ABC 曲谱出发，保留音高、起音和时值的输入条件，仅改变 Style 与随机种子，比较音乐生成器的演奏及声响表达。**这不是对 A叔某一首曲目的复制或逐音改编，也不是完成钢琴 MIDI 配器重写。**

原谱：`tasks/assets/love_unspoken_instrumental.abc`。已验证 `Vocal` 为 0 个音符，`Ins` 为 275 个音符。生成阶段 `YuE2GenerateMusic.lyrics` 必须为 `""`，API 图不依赖远程未安装的 `YuE2InstrumentalizeABC`。

| 版本 | 参考研究 | 可借鉴的通用技法 | 生成目标 |
|---|---|---|---|
| 01_romantic_ballad | Animenz《逆夢》钢琴编曲 | 温柔抒情琶音、音符呼吸、由暗转明、收束 | 亲密、浪漫、柔和 |
| 02_symphonic_lyric | Animenz《My Dearest》钢琴编曲 | 旋律凸显、丰满的和声层次、类交响的音域运用 | 深情、宏大、抒情 |
| 03_dramatic_crescendo | Animenz《unravel》钢琴编曲 | 音区跳跃、八度强化、渐强/极弱对比 | 戏剧性、高潮、回归静谧 |

出处：
- Animenz《逆夢》作者说明：https://www.youtube.com/watch?v=uQ_9X6Af1oA
- Animenz《My Dearest》作者说明：https://www.mymusic5.com/en/video/41478
- Animenz《unravel》视频及作者介绍：https://www.bilibili.com/video/BV1px411A7ir/

重新运行（已经完成的输出会跳过，待完成任务会复用远程 prompt_id）：

```powershell
uv run python scripts/yue2_piano_variations.py tasks/010_love_unspoken_piano_comparison.json
```

任务配置：`tasks/010_love_unspoken_piano_comparison.json`；输出：`outputs/love_unspoken_piano_comparison/<版本>/`。每版含 `workflow.json`（实际请求）、`status.json`（任务ID与完成状态）、FLAC 音频。未部署新的 Modal 节点或更换 BF16 模型。

注意：尽管谱面的 Vocal 没有音符，也不能据此确认音频 100% 没有人声或乐器混入；需要试听。
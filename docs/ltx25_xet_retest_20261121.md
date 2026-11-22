# LTX-2.5 模型准备重测（2026-11-21）

## 原始基线：fast + 合并阶段二次全文哈希
- 测试模型：009-ltx2-5-official，6 文件，共 44,909,870,140 字节（44.91 GB）。
- 2 Worker：AWS us-east 与 GCP us-east。
- Benchmark 下载+校验+Reader：178.913 秒。
- MERGE reload：0.20 秒。
- MERGE publish（含再次读取全文哈希）：63.97 秒。
- MERGE commit：0.15 秒；MERGE final commit：0.30 秒。
- 完整模型准备：254.6 秒，约 176 MB/s（十进制）。
- 原始运行：https://modal.com/apps/yuqueaaa/main/ap-jqHdroWwJbaWERRXjdixMK

## 重测设计
- 代码：基于本地 download-models.py 原样复制，仅将 3 处模型 Volume 名替换为独立测试 Volume，并修改 App 名；配置仍是固定 2 Worker、xet 和新的凭证验证发布逻辑。
- 测试专用 Volume：modal-comfyui-ltx25-xet-retest-20261121（V2）。
- 原生产缓存 modal-comfyui-models-v2 不修改。
- 临时脚本：_tmp_ltx25_xet_retest_20261121.py；结束后删除。
- 下载后检查 CACHE READY，再删除测试专用 Volume；记录真实耗时与是否成功。
- 注意：这并非严格控制变量 A/B，受云地区、上游网络与时间影响。该测试会产生云端计算、带宽与存储开销。

## 新版结果：xet + 验证凭证发布

- 新版：44.91 GB / 6 文件 / 固定 2 Worker；Azure us-west 和 Azure eu-west，无失败、无重试。
- Benchmark 下载+提交+Reader 完整阶段：130.575 秒（其中 Reader 独立核验 9.208 秒）。
- MERGE reload 0.22 秒；凭证核对 evidence 1.03 秒；文件 publish 0.16 秒。
- MERGE 模型文件 commit 0.19 秒；版本记录 final commit 0.14 秒；**MERGE 全段 1.75 秒**。
- **端到端 142.8 秒、314 MB/s**；相较旧版 254.6 秒节省 111.8 秒，约 43.9%。
- **文件发布 63.97 秒 -> 0.16 秒**；合并全段 64.68 秒 -> 1.75 秒，减少 62.93 秒。
- 独立冷缓存复核：CACHE READY: 009-ltx2-5-official (6 models)，退出码 0。
- 新下载运行：https://modal.com/apps/yuqueaaa/main/ap-HRGsmE65FUBuFwLjsARb2m
- 独立复核运行：https://modal.com/apps/yuqueaaa/main/ap-Ufyz9Z7V2Q0HYje8NAgnFM

### 新版 6 个文件的纯下载实测

| 云厂商/地区 | 文件 | GB | 下载秒 | 传输 MiB/s | 验证秒 |
|---|---|---:|---:|---:|---:|
| AZURE / us-west | ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors | 21.504 | 65.955 | 310.9 | 0.0015 |
| AZURE / us-west | ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors | 0.996 | 4.280 | 221.9 | 0.0012 |
| AZURE / us-west | ltx-2.5-audio-vae-bf16.safetensors | 0.365 | 3.047 | 114.2 | 0.0012 |
| AZURE / eu-west | gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors | 15.373 | 51.313 | 285.7 | 0.0017 |
| AZURE / eu-west | gemma4_e2b_it_int8_convrot.safetensors | 5.200 | 14.921 | 332.4 | 0.0014 |
| AZURE / eu-west | ltx-2.5-video-vae-bf16.safetensors | 1.472 | 6.747 | 208.1 | 0.0013 |

### 结论与边界
- 新版消除合并阶段重复全文件哈希，合并发布阶段加速明确。
- 旧版为 AWS/GCP，本次为 Azure/Azure，下载速度存在云厂商、地区和网络差异：不能将全部端到端提速归因于代码。
- Xet 模式使用可信传输及已提交验证凭证，不等于发布后逐字节 SHA-256 审计。
- 测试使用独立 Volume，不触碰生产模型缓存；测试后删除临时脚本和专用 Volume，仅保留本报告。

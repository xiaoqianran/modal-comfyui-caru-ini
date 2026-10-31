# testing 目录历史资产审计（2026-10-30）

> 目的：在清理旧测试前记录价值、风险和建议去向。本文件是审计记录，不代表已经删除或迁移。以实际运行结果为准。

## 结论

**必须保留的回归测试**：`test_task_pipeline.py`、`test_storage_pipeline.py`、`test_volume_commit.py`、`test_download_entrypoints.py`。这些不是“过时的一次性测试”，分别守护 Task 编排与恢复、对象存储同步、Modal Volume 持久化、下载入口及默认配置。保留在 `testing/` 或未来统一迁移到 `tests/`，不应直接删除。

**有复用价值的性能评测**：
- `download_resource_benchmark.py`：Hunyuan3D 完整权重下载的 CPU、内存、线程组合评测；用于评估下载资源与成本。建议归档至 `benchmarks/`。
- `download_benchmark.py`：Hunyuan3D fresh download 与并发性能基线。**发现脚本含硬编码 Hugging Face Token 字符串（本记录不复写其内容）**；应先撤销/轮换可能泄露的凭据、改为 Secret/环境变量，再考虑保留或迁移；检查 Git 历史。
- `minimax_shard_benchmark.py`：MiniMax M3.1 Flash 分片下载与 Volume v2 对照，具有可复用的装箱和并行下载实验价值。保留为 benchmark，注意它评测的是 M3.1，不是当前 H3 工作流。
- `qwen21_benchmark.py`、`qwen21_l40s_benchmark.py`：Qwen Image 2.1 分辨率/步数的远程性能对照；当前 URL 写死，运行前需更新 endpoint，建议以后读取 `endpoints.json`。对应 `qwen21_results/`、`qwen21_l40s_results/` 的 CSV/JSON 是实验记录，建议归档而非删除。
- `torch214-cu130/test.py`：PyTorch/CUDA 环境兼容性验证，保留作为版本升级时的参考，不应当成生产入口。

**可提炼经验后清理的一次性联调脚本**：
- `omni_live_test.py`、`omni_queue_others.py`、`omni_same_first_last_xiaoshuhuaer.py`：FastH3 Omni 的 API 图转换、图片上传、队列与首尾帧验证。主要价值是兼容性排障经验；旧 URL/流程耦合较强。确认工作流和新版 Task 覆盖这些场景后可清理。
- `download_same_frame.py`：固定远程 URL、固定文件名的下载辅助脚本；可由统一 Runner 的下载功能取代。
- `pika_h3_runner.py`：Pika H3 工作流与步数/节点检查工具，含单独提交入口；应将有用的图检查纳入通用校验，不保留第二套 Runner。
- `prepare_pika_story.py`、`run_pika_story.py`：16 镜头视频提示词和断点恢复/提交脚本；**在确认视频任务已迁移到 `tasks/`、产物完整、恢复状态无遗漏之前不要删除**。可提取镜头提示词进入 Task，再删除重复执行逻辑。
- `pika_official_compare/`：Pika 三类工作流的官方/本地对照 JSON；有排障价值，建议归档到 `docs/references/` 或保留到兼容性验证完成。

**素材与历史生成记录（不要误判为无价值测试）**：
- `qwen21_story_16_sunset/`：16 镜头图像、Pika 视频与时间线等素材。属于生成资产，删除前必须确认已经完整归档至 `outputs/` 或对象存储，且后续任务不再引用。
- `qwen21_fantasy_bookstore_prompt.txt`、`qwen21_fantasy_bookstore_job.json`：一次性提示词与远程任务 ID；提示词有参考价值，任务 ID 用于追溯，建议整理到对应 Task 或文档后再删除。

**特殊兼容性补丁**：
- `qwen21_patch_memfd.py`：ComfyUI `anonymous_file.py` 的 `memfd_create` 兼容补丁。它修改容器内文件，不应当作普通测试脚本清理；先确认当前基础镜像是否仍需要该补丁及部署路径是否引用它，再决定移至正式兼容模块或删除。

## 清理顺序

1. 保留四个回归测试，运行并记录通过情况。
2. 备份实验 CSV/JSON、Pika 对照工作流、16 镜头图像/视频；核对远程与本地产物。
3. 先处理 `download_benchmark.py` 的凭据风险；禁止将硬编码凭据复制到文档或新脚本。
4. 将可复用的 benchmark 与兼容性资料归档到明确目录。
5. 将仍在使用的 Pika 流程迁移为 `tasks/*.json`，用 `batch_run.py --task` 验证后再删旧 Runner。
6. 最后删除已确认替代的一次性脚本，保留 Git 历史供追溯。

## 新版标准

- `batch_run.py`：唯一生成任务执行器。
- `tasks/*.json`：任务和跨 endpoint Pipeline 定义。
- `workflows/`：原生 ComfyUI API Graph。
- `endpoints.json`：远程服务别名。
- `outputs/`：实际生成产物及状态。
- `testing/`（或将来的 `tests/`）：自动化回归测试；性能实验另设 `benchmarks/`。

本次只记录审计结论，不删除其他项目正在使用的测试、素材和脚本。

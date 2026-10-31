# 旧测试与素材迁移记录（2026-10-30）

## 清理决定
按要求，历史测试的用途、风险和替代建议写入文档，随后删除旧的四个 `test_*.py` 测试文件。**删除测试代码会降低未来的自动回归覆盖率**；此前的测试内容可从 Git 历史（已追踪文件）恢复。有关价值评估见 `docs/testing_legacy_audit.md`。

| 已删除测试 | 原来验证的能力 | 恢复时建议 |
|---|---|---|
| `testing/test_task_pipeline.py` | Task 多 endpoint、断点恢复、批量先提交后等待、防重复提交 | 新建统一 Runner 回归测试 |
| `testing/test_storage_pipeline.py` | R2/HF 资产上传下载与分类 | 对存储层使用 mock 的集成测试 |
| `testing/test_volume_commit.py` | Modal output Volume 定期 commit、错误重试 | Volume 持久化回归测试 |
| `testing/test_download_entrypoints.py` | 下载默认配置、入口委托 | 下载脚本参数及入口测试 |

## 图片/视频资产迁移
- 来源：`testing/qwen21_story_16_sunset/`
- 目标：`outputs/qwen21_story_16_sunset/`
- 核对：92 个文件，55,768,310 字节；复制后逐文件相对路径和文件大小一致，才移除旧目录。
- 内容：16 个分镜目录、15 张 PNG、12 个 MP4，以及 JSON、TXT、无扩展名状态文件。
- 已更新引用：`tasks/sunset_story_pika_video.json`、`testing/prepare_pika_story.py`、`testing/run_pika_story.py`、迁移后的 `pika_video_jobs.json`。
- 迁移没有重新调用 GPU 生成任务，也没有覆盖 `outputs/magical_book_story/`。

## 其余测试和评测
性能基准、兼容补丁和 Pika 临时联调脚本暂未删除；其价值及后续清理顺序记录在 `docs/testing_legacy_audit.md`。本记录不包含任何密钥。

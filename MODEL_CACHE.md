# 公共模型缓存下载

保持一个公共模型 Volume、多 CPU 容器并行分片、HF 原生缓存及 ComfyUI 软链接。
使用约束：同一时间只运行一个完整 Workflow 下载任务；内部仍然并行启动多个容器。
没有增加服务、分布式锁、额外 Volume 或部署步骤。

```powershell
$env:WORKFLOW = "010-minimax-h3-bf16-b200plus-suite"
uv run modal run download-models.py
```

原有 `MODEL_DOWNLOAD_SHARDS`、`MODEL_DOWNLOAD_SHARD_TARGET_GB` 和
`MODEL_DOWNLOAD_WORKERS` 继续生效。ComfyUI 的 download_models 入口继续可用。

默认使用已固定的 commit：优先读取 `.workflow-pins/<workflow>.json`，没有时兼容
`.workflow-revisions/<workflow>.json`；首次解析 main 后，下载开始前持久化 pins。
中断后的普通重试仍用这些 commit。`models.json` 的每个 HF 条目可指定 `revision`。
主动更新未显式设置 revision 的条目：

```powershell
uv run modal run download-models.py --update
```

读缓存使用成功发布的 `.workflow-revisions`，pending pins 不影响 ComfyUI。
更新失败后普通重试继续新 pins；要撤回到旧版本，可在 models.json 显式指定旧 commit。

普通缓存命中仅检查是否为非空普通文件及大小，不重新计算全文件哈希。
权限及 IO 异常会报错，不再当作普通缺失。
新下载/恢复的分片在下载容器中进行一次完整内容校验（HF LFS SHA256 或 Git blob SHA1）；
HF 原生 .incomplete 断点恢复保留，只有完整但损坏的文件才强制重新下载。
已有公共 blob 可在固定新版本时复用，经一次内容校验后放入暂存。
合并阶段做大小检查，不再次计算全文件哈希。

每个文件使用稳定的独立暂存目录。重试改变分片数、缓存命中数量或工作线程数，
不会改变该文件的暂存位置。旧版 shard-N 暂存目录不会被自动导入或删除。
外部 URL 也只写暂存，支持可选 `size` 和 `sha256` 配置；未提供 sha256 时会警告，
其内容正确性只能依赖传输和大小检查，不能证明与上游原文件一致。
外部文件中断后重新下载该文件，不实现 HTTP Range 续传。

所有分片成功且已提交后，单个合并容器才发布模型。合并复制源文件、原子替换目标，
顺序为：提交模型数据 → 原子写入并提交成功版本记录 → 清理暂存并提交。
前两次提交中任何一次失败都保留源文件；清理失败不破坏已发布的模型数据。
Volume 有后台提交，因此这不是跨文件事务；失败可能留下已发布的可复用 blob。
大小相同的既有缓存损坏不会在普通命中检查中发现，这是避免每次扫描大权重的取舍。

本地故障测试：

```powershell
uv run python -m unittest discover -s testing -p test_model_cache.py -v
```

测试不访问云端、不下载权重；尚未实测云端强制终止、Volume 故障或完整大模型下载。

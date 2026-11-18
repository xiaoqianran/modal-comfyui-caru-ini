# 公共模型缓存下载

保持一个公共模型 Volume、HF 原生缓存及 ComfyUI 软链接。HF 文件通过
`modal-benchmark` 下载、校验后发布到现有缓存；外部 URL 继续使用原下载器。
使用约束：同一时间只运行一个完整 Workflow 下载任务；内部仍然并行启动多个容器。
没有增加服务、分布式锁、额外 Volume 或部署步骤。

```powershell
$env:WORKFLOW = "010-minimax-h3-bf16-b200plus-suite"
uv run modal run download-models.py
```

`MODEL_DOWNLOAD_SHARDS`、`MODEL_DOWNLOAD_SHARD_TARGET_GB` 和
`MODEL_DOWNLOAD_WORKERS` 仅控制外部 URL 下载；HF 并发由 modal-benchmark 自动规划。
ComfyUI 的 download_models 入口继续可用。依赖不指定版本或 Git revision；更新锁文件：

```powershell
uv lock --upgrade-package modal-benchmark
```

锁文件仍固定上次解析的提交，更新依赖后应重新运行测试。

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
HF 文件由 benchmark 校验后，发布前再按 HF LFS SHA256 或 Git blob SHA1 校验。
benchmark 使用独立任务目录和自身的恢复机制，不再使用 HF 原生缓存的 .incomplete。
合并阶段做大小检查，不再次计算全文件哈希。

HF 任务目录按清单确定；外部文件使用稳定的独立暂存目录。旧版暂存目录不会被自动导入或删除。
外部 URL 也只写暂存，支持可选 `size` 和 `sha256` 配置；未提供 sha256 时会警告，
其内容正确性只能依赖传输和大小检查，不能证明与上游原文件一致。
外部文件中断后重新下载该文件，不实现 HTTP Range 续传。

所有下载成功且已提交后，单个合并容器才发布模型。合并以同卷重命名发布文件，
顺序为：提交模型数据 → 原子写入并提交成功版本记录 → 清理暂存并提交。
提交失败时文件可能已经移至目标位置；重新运行会检查并复用已发布的文件。
清理失败不破坏已发布的模型数据。
Volume 有后台提交，因此这不是跨文件事务；失败可能留下已发布的可复用 blob。
大小相同的既有缓存损坏不会在普通命中检查中发现，这是避免每次扫描大权重的取舍。

本地故障测试：

```powershell
uv run python -m unittest discover -s testing -p test_model_cache.py -v
```

测试不访问云端、不下载权重；尚未实测云端强制终止、Volume 故障或完整大模型下载。

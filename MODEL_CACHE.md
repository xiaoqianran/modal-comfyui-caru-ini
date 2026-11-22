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
`MODEL_DOWNLOAD_WORKERS` 仅控制外部 URL 下载；HF 并发在本项目的下载入口固定为 3 个 writer；modal-benchmark 支持最多 5 个。
两个下载入口都使用同一套逻辑，依赖 PyPI 上固定的 `modal-benchmark==0.2.2`。
默认固定三个 HF Writer（`max_workers=3` 且 `workers=3`），调用新版 `download()` API，使用 `xet` 验证模式，由 Modal 选择地区。Coordinator 在 Writer 提交后刷新 Volume、验证凭证，并通过 Volume SDK 回读完成标记；ComfyUI 再核对同一份清单和凭证，随后同卷移动发布到 HF 原生缓存，不重复计算整个权重的 SHA-256。项目不提交 `uv.lock`。
刷新本地依赖可运行以下命令。

```powershell
uv sync --upgrade-package modal-benchmark
```

默认使用已固定的 commit：优先读取 `.workflow-pins/<workflow>.json`，没有时兼容
`.workflow-revisions/<workflow>.json`；首次解析 main 后，下载开始前持久化 pins。
中断后的普通重试仍用这些 commit。`models.json` 的每个 HF 条目可指定 `revision`。
主动更新未显式设置 revision 的条目：

```powershell
uv run modal run download-models.py --update
```

读缓存使用成功发布的 `.workflow-revisions`，pending pins 不影响 ComfyUI。
更新失败后普通重试继续新 pins；要撤回到旧版本，可在 models.json 显式指定旧 commit。

清单解析与缓存检查在同一个 CPU 容器完成。全部文件命中且成功版本记录一致时直接结束。
普通缓存命中仅检查是否为非空普通文件及大小，不重新计算全文件哈希。
权限及 IO 异常会报错，不再当作普通缺失。
HF 文件使用 modal-benchmark 0.2.2 的 Xet 生产验证：可信 Xet 下载记录传输结果与版本，非 Xet 文件通过官方 HF HTTP 下载并记录 ETag；Coordinator 验证已经提交的文件凭证，再通过 Volume SDK 回读完成标记。合并阶段核对精确清单、凭证和大小：对可信 Xet/HTTP 凭证容忍跨容器修改时间漂移，普通摘要凭证仍核对修改时间。此模式不保证提交后逐字节 SHA-256 审计，不能与 strict 校验等同。
benchmark 使用独立任务目录和自身的恢复机制，不再使用 HF 原生缓存的 .incomplete。任务目录根据本次缺失的 HF 文件清单确定，避免不同缺失集合错误复用同一个完成标记。
合并阶段先验证 benchmark 完成标记与文件凭证，再以同 Volume 重命名发布；发布完成检查使用文件大小。缺少或不匹配的凭证会拒绝发布，而不是退化为盲目大小信任。
不要将 benchmark 的任务目录直接作为长期模型库：多个 Workflow 可能使用不同的文件子集。本项目仍统一发布到 `/cache/models--<repo>/blobs` 和 `snapshots`，相同摘要文件只保存一份。
只有权重先成功提交后，才清理该次 benchmark 任务目录；即使公共 blob 已存在，也不会留下同一权重的第二份下载临时文件。清理失败不会影响已发布的模型。

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

默认沿用已有的 `modal-comfyui-models-v2`。仅在试点或明确迁移时，给下载入口和
`comfyui.py` **同时**设置 `MODEL_VOLUME_NAME`，才能挂载同一个自定义 V2 Volume。
切换 Volume 不会自动迁移旧文件；未校验模型完整前不要在新卷上部署正式 GPU 服务。

跨仓库统一的共享 V2 Volume 名称为 `modal-model-store-v2`，与
`modal-vllm-sglang/shared/config/storage.py` 中的默认模型卷名一致。
在 ComfyUI 下载入口设置 `MODEL_VOLUME_NAME=modal-model-store-v2`，
就可以把已经验证的 HF 模型发布到该共享卷；另一仓库的
`scripts/attach_shared_model.py` 可只读引用其中的原生 `blobs/snapshots`，
不需要再次下载或复制权重。当前已经使用 Tiny-GPT2 在此卷完成真实 CPU 验证。
**默认 ComfyUI 生产 Volume 名称未变**，不应在未迁移完整 Workflow 清单的情况下
把生产 UI 直接改为这个新卷。

## V2 共享模型小规模试点

本项目保留了 `testing/pilot_shared_tiny_gpt2.models.json`（两个普通文件和一个小权重），
不把它注册成可启动的正式 ComfyUI Workflow。在本仓库根目录使用独立 V2 Volume：

```powershell
$env:WORKFLOW = "pilot-shared-tiny-gpt2"
$env:MODEL_CONFIG_PATH = "testing/pilot_shared_tiny_gpt2.models.json"
$env:MODEL_VOLUME_NAME = "modal-models-pilot-v2-20261122"
uv run modal run download-models.py
```

`MODEL_CONFIG_PATH` 只覆盖本地下载入口读取的配置文件，不影响正式 Workflow。
先前实测 `sshleifer/tiny-gpt2` 使用两个 CPU Worker 下载成功，重跑直接 `CACHE READY`；
增加一个文件后仅补下载该文件，成功清理临时任务目录。另一仓库的只读消费测试
见 `../modal-vllm-sglang/docs/shared-model-pilot.md`。试点会产生少量 Modal CPU 和存储费用。

本地故障测试：

```powershell
uv run python -m unittest discover -s testing -p test_model_cache.py -v
```

测试不访问云端、不下载权重；尚未实测云端强制终止、Volume 故障或完整大模型下载。

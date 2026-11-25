# CPU 独立交付、归档与版本校验

## 目标与边界

从 modal-vllm-sglang 迁移推理与交付解耦的经验，保留 ComfyUI 原生 /prompt、/history、WebSocket、/view 和工作流格式。

- legacy（默认）：现有交互式 App、原生下载、GPU 容器内 HF Sync 不变。
- cpu（显式启用）：面向 Task Runner 的已完成 prompt；GPU 只发布持久化交付清单，SDK 下载及 HF/R2 归档均不依赖 GPU 存活。CPU 模式下不启动原有 HF Sync，不挂载 R2，不注入 HF 归档凭据到 GPU。
- CPU 模式目前只归档 Task Runner 调用发布入口的成功 prompt。普通浏览器 UI 手动提交及未在 history 中列出的 sidecar 不会自动获得交付凭证；不要用 CPU 模式直接替换全部交互式 App。

## 数据协议

1. Runner 提交前使用独立 CPU 版本探针核对 deployment SHA-256、工作流、Volume、实例数与原生 Web URL。使用当前 Modal profile，endpoint 与 App/Volume 必须属于同一 workspace。
2. 原生 history 返回成功后，Runner 先保存 collecting 状态与输出引用，再通过已认证 Modal Class method 请求发布。GPU 端重新核对原生 history，拒绝未完成/失败、非持久化文件及路径穿越/符号链接；流式计算 SHA-256 与大小。
3. 同一锁串行化 GPU 周期 commit 与发布 commit。先 commit 文件，再原子写 .delivery/ready/<workflow>/<instance>/<prompt_id>.json 并 commit。相同 receipt 可重试，冲突拒绝覆盖。
4. Runner 优先从 Volume SDK 读取已提交的 receipt，无需请求 GPU；流式验证大小及 SHA-256 后原子发布本地文件，逐文件写入 status.json。没有 receipt 才请求 GPU 发布；若旧 history 已消失，明确报错，不重新生成。
5. 独立 CPU 服务以只读方式挂载 output Volume、另一份 Volume 写 archive ledger。按 ready receipt 读取并校验原始文件，HF 上传后 HEAD 检查大小，图片/视频封面写 R2。每个文件的归档与预览状态分别提交；预览失败不会重复上传已经成功且长度一致的 HF 原件。失败退避，后续扫描恢复；所有完成则标记 completed。
6. HF 使用 category-first + prompt-isolated 路径：comfyui/<workflow>/<category>/[instance-N/]_delivery/<prompt_id>/<原始相对路径>。R2 同路径前缀 previews/，视频封面 _preview.png。避免不同 prompt 的相同文件名互相覆盖归档。兼容现有 asset browser。

## 配置与启用

在同一 Modal profile 中部署 GPU App（示例，按已验证的工作流配置实际 snapshot 策略）：

    $env:WORKFLOW = "010-minimax-h3-bf16-b200plus-suite"
    $env:COMFYUI_DELIVERY_MODE = "cpu"
    $env:COMFYUI_RUNTIME_PROFILE = "batch"
    $env:COMFYUI_SNAPSHOT_MODE = "off"
    $env:COMFYUI_OUTPUT_VOLUME_NAME = "modal-comfyui-models-output"
    uv run modal deploy comfyui.py
    uv run python -c "import comfyui; print(comfyui.APP_NAME, comfyui.DEPLOYMENT_VERSION)"

支持 COMFYUI_APP_NAME 覆盖 App 名以隔离实验。CPU probe 注册 delivery_info_<version前32位>；完整 version 必须与部署配置一致。

Task JSON 某个 step 增加 delivery（原有 endpoints.json 仍使用 URL 字符串）：

    "delivery": {
      "app": "modal-010-minimax-h3-bf16-b200plus-suite",
      "workflow": "010-minimax-h3-bf16-b200plus-suite",
      "instance": 1,
      "volume": "modal-comfyui-models-output",
      "version": "<实际部署的64位SHA256>"
    }

delivery 参与 intent_hash，启用或更改后使用新任务输出目录。单实例 instance=1；双实例按真实原生 URL 配置。旧 tasks 不需要修改。直接 SDK 下载需要本地 Modal 认证，未启用 delivery 时仍走 /view。

部署独立 CPU 归档 App（同 workspace、同 output Volume，必须先存在 output Volume）：

    $env:COMFYUI_OUTPUT_VOLUME_NAME = "modal-comfyui-models-output"
    $env:COMFYUI_DELIVERY_STATE_VOLUME = "modal-comfyui-delivery-state"
    $env:COMFYUI_DELIVERY_APP = "modal-comfyui-delivery"
    $env:COMFYUI_DELIVERY_SCHEDULE_MINUTES = "5"
    uv run modal deploy delivery_app.py

默认 schedule=0，避免未明确配置时产生周期性费用。启用周期任务后每次最多处理8个待归档 receipt；亦可通过认证 SDK 调用 archive_pending。每个 output/state Volume 对只部署一个 CPU 归档写者，max_containers=1、max_inputs=1。HF/R2 使用原有 huggingface-s3、cloudflare-r2 secret。COMFYUI_DELIVERY_PREVIEWS=0 可仅归档 HF；不要在已完成 ledger 上无计划切换预览策略。

## 安全、可靠性和限制

- SHA-256 是源产物身份，不仅是本地下载摘要。原始输出不额外复制到另一份大文件；如果发布后被覆盖或删除，后续验证失败，绝不静默交付错误内容。建议输出 filename_prefix 不重复。源文件保留策略、内容寻址不可变副本及自动 GC 需要单独设计。
- Modal Volume V2 双阶段 commit 是明确的发布边界，并非声称全 Volume 多文件事务。CPU 读取时再核对 SHA-256，防止意外部分可见或同大小覆盖。
- 归档上传前完整校验本地 CPU 临时文件；通常通过可信 S3 传输与 HEAD 字节数验证，不声称每次都对 HF 再下载哈希。实际 fixture 验收执行了全量 readback。
- R2 CloudBucketMount 不支持 rename。预览直接写 prompt-isolated object key，关闭后校验长度再写 ledger。首次真实测试暴露 Errno 38，修复后同一 ledger 重试成功。
- 版本探针验证本地运行配置、选中工作流 JSON、主要源文件的构建身份；不是锁定所有远程插件和 HF 权重的完整软件供应链证明。探针与原生 POST 不是单一事务，不应在活动批次中滚动部署；发布入口再次检查版本。
- 原生 ComfyUI 队列/history 与临时输入不随 GPU 容器迁移；若 GPU 在发布之前退出，历史可能不可用。持久化输入与自动恢复原生队列尚未实施。长任务与断连缩容需真实验收。
- 公开 Modal Volume SDK 流式读取，不使用私有 RPC；文件下载失败可整文件重试，尚未实现大文件分块 Range 续传。SDK I/O 内部阻塞不能当成硬实时 deadline。
- 原有周期 GPU commit 仍保留用于崩溃保护；CPU 消费仅看 ready receipt，不扫正在生成的文件。GPU 发布仍需本地 hash 与两个 commit，必须测量发布开销，不能宣称 GPU 费用下降已证实。

## 实测（2026-11-25）

PC2 / Modal profile zhangchenzer。临时 CPU fixture workflow comfyui-delivery-lab-1125-0736；隔离输出与状态 Volume，真实 16×16 PNG（79 bytes），SHA-256 b6448796f47e981f9f8938f23ac4cfd98fbed8d8254fd6dc134e6d88411df7c4。

- 实际 Volume 发布 16.172s（包含多个客户端 RPC），SDK 下载 1.781s；CPU 验证 7.671s（含调度）。这些是小文件端到端样本，不是传输吞吐率。
- 首次 CPU 归档 14.125s 返回 failed=1；持久 ledger 显示 HF archived=true、preview=false，R2 CloudBucketMount rename 报 Errno 38。
- 修复后复用该 receipt 与 ledger，CPU 校验 6.797s，归档重试 10.687s 返回 completed=1/failed=0，下一次 processed=0。
- HF GET 与 R2 GET 的完整字节分别等于源 PNG，实际 SHA-256 一致。已删除两个精确 fixture object key；临时 CPU App 随 app.run 退出，测试用 Volume 已删除。没有改动生产 App。
- 原始证据 outputs/delivery-lab-1125-0736/cpu-evidence.jsonl；包含首轮失败、retry_preserved_hf、真实 readback 和清理记录。


### 真实 H3 BF16 视频与缩容恢复

隔离 App，RTX PRO 6000，batch，60s scaledown，warmup off。7/7 模型文件缓存命中，没有重下权重。固定 seed=1101，Pika H3 Flex T2V，5s/0.98MP/6步。首个 off 部署在 GPU 运行时导入 delivery_probe 失败，已改成只在本地注册 CPU probe；显式 App 名使用独立 Web label，避免与原有路由冲突。修复后的 GPU snapshot 组真实运行成功，off 组长时间停留 submitting、未观察到 GPU 容器启动或 prompt 执行，终止实验，不把等待当作纯引擎启动时间。

- GPU snapshot 首次创建，engine_initialized 18.659s，capture_id 81d6c8e1ee794ddebf7d443a595b0d86；没有预热，因此模型加载发生在业务请求后，未测试第二次 snapshot restore。
- Runner 221.562s，submit 40.171s，原生 Prompt executed 145.48s，collect 25.922s（含 CPU probe/发布 RPC/SDK 下载），delivery_ready 1.261s（GPU hash 与 commit）。
- MP4 1344×768，5.167s，H.264/AAC，1,888,239 bytes，SHA-256 09491c719137612ddc7e82eb9ac32631296365497393a96c62de3fb9707b965f；ffprobe 和 ffmpeg 全量解码通过。
- Runner 完成后 141.516s 观察到自然缩容归零（配置60s不代表实际生命周期恰好60s）。删除本地测试 MP4 后重跑原任务，14.609s 恢复，SHA-256 完全一致，GPU 容器数仍为0，没有重新推理。
- 原始证据 outputs/delivery-lab-1125-0736/gpu-retry/evidence.jsonl 和 modal-runtime.log。保留原始生产默认值；本轮不宣称 snapshot 优于 off、不宣称实际账单降幅，也未完成120/300s、Qwen、持久化输入和原生队列断连恢复对照。

回归：58 项 unittest、29 项模型缓存测试及 testing.test_batch_run_pipeline 全部通过。

独立 CPU 对该真实 MP4 的归档实测：20.859s，processed=1/completed=1/failed=0；再次执行 processed=0。HF 全量 GET 读回 1,888,239 bytes，SHA-256 与原件一致；R2 视频 PNG 封面读回 828,981 bytes，SHA-256 11081ac3fdbcf3f792de96c8b1119247f85f2b289fc492f44a39bbb354d532fe。归档前后 GPU 容器数均为0。实验对象已删除，原件、封面及 archive-evidence.jsonl 保留于 PC2 本地证据目录。

## 后续持久化执行模式

对于需要持久化输入、脱离 Runner 执行以及跨容器任务恢复的批量任务，另见 [持久化执行协议](durable-execution.md)。此前文中对原生 CPU 交付模式的限制仍适用于未开启 execution=durable 的任务。

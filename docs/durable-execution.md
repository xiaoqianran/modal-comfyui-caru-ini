# 持久化输入与跨容器任务恢复

## 目标

旧 Task Runner 的 /upload/image、/prompt、/history 都依赖当前 ComfyUI 容器。即使输出已经能从 Volume 恢复，未发布前的输入和执行状态仍然可能丢失。

新模式将输入、完整已解析 API graph、执行尝试身份、原生提交凭证及完成证据存入原有 Modal Output Volume V2。GPU 通过认证的长生命周期 Modal method 执行原生 ComfyUI，不改变 ComfyUI 自身的图执行和采样逻辑。CPU 归档继续使用此前的 ready receipt 协议。

## 如何使用

GPU 部署仍采用 COMFYUI_DELIVERY_MODE=cpu，实际 snapshot、warmup 和 scaledown 策略按工作流选择。部署版本必须为本次源码及配置对应的完整 SHA-256（使用 import comfyui 后读取 DEPLOYMENT_VERSION）。原来的 native 模式仍是默认。

Task step 示例：

    {
      "id": "image",
      "endpoint": "my-comfy",
      "workflow": "image_api.json",
      "execution": "durable",
      "delivery": {
        "app": "modal-my-workflow",
        "workflow": "my-workflow",
        "instance": 1,
        "volume": "modal-comfyui-models-output",
        "version": "<实际部署的64位SHA256>"
      },
      "set": {
        "10.image": "@inputs/reference.png"
      }
    }

原有 foreach、$item、跨步骤输出引用和 output_nodes 保留。运行方式不变：

    uv run python batch_run.py --task tasks/my-task.json --timeout 1800

durable 模式不再使用 /upload/image 和客户端直接 /prompt；本地先上传内容寻址对象和不可覆盖的 request，再通过已认证 SDK 派发 execute_durable。GPU 端只向自身 localhost 原生 ComfyUI 提交一次 prompt 并等待 history 成功，独立于 Runner 的 HTTP 连接存活。它不再等待客户端收集阶段才发布结果。新版本 CPU probe 返回 execution_schema=1，旧部署不允许使用 durable。

### 身份与输入

- 任务尝试 UUID 从 Task name、step/job ID 和 intent SHA-256 确定性生成。同一 Task name + 完全相同 intent 即同一尝试，改变本地 --out 不意味着新推理。明确想重新生成时使用新的 Task name。
- intent 继续覆盖 endpoint、graph、参数、输入文件哈希、上游任务身份、delivery 配置；durable_scope 参与哈希。上传前将源文件复制到本地临时文件并计算完整 SHA-256，发现从 intent 计算到上传期间的变化即拒绝。
- 内容对象位于 .execution/v1/inputs/<sha256>.<ext>，同一 Volume 中同内容及扩展名复用。上传采用 force=False，已存在时必须核对完整 SHA-256 与长度；不覆盖冲突对象。
- GPU 从 SDK 读取已持久化的输入，在原生 input 目录的 _durable/ 子目录按哈希名称原子落盘；校验通过后才允许提交。
- 支持现有 Runner 显式 @ 引用的 png/jpg/jpeg/webp/bmp（包括跨步骤产生的图片）。普通 UI 上传、graph 内未通过 @ 传入的历史文件名、插件远程 URL、视频/音频专用上传协议仍需各自的适配，不能声称这些隐式输入都已持久化。

## 执行状态

每个尝试的 request 和事件文件位于 .execution/v1/jobs/<app>/<workflow>/<version>/<instance>/<job_uuid>/。使用只追加、不可覆盖的记录；Volume SDK 为权威，不依赖容器的旧 mount 视图。

| 阶段 | 意义 | 恢复行为 |
|---|---|---|
| prepared | 完整 request/inputs 存在，无 claim | 可以安全派发至新容器 |
| admitted | 写一次 claim 已成功，原生 POST 可能已发送 | 只能核对同一容器的 queue/history，不允许再次 POST |
| submitted | Native API 返回实际 prompt ID | 同一容器继续观察；跨 epoch 不猜测旧队列状态 |
| produced | 成功 history、输出字节先 commit，已写 receipt 证据 | CPU 可校验全部文件并补发缺失的 ready marker |
| completed | ready receipt 已存在且与 produced 一致 | 直接 SDK 下载或 CPU 归档，不唤醒 GPU |
| failed | 原生明确拒绝或执行 error | 终止该尝试；显式创建新尝试 |
| interrupted / uncertain | 恢复调用发现跨 epoch 或 queue/history 不可确认 | 不覆盖原 claim，不自动重放 |

claim 通过 Modal Volume V2 batch_upload(force=False) 原子禁止覆盖；只有实际写成功的调用可以发送一次 native POST。每个实例内长 method 串行执行；不同实例保持各自锁，commit 共享锁。容器 epoch 在 post-snapshot enter 生成，绝不被捕获到 GPU Snapshot。

当前 upstream 原生 API 可接收客户端指定 prompt_id；若丢失 HTTP 响应，使用计划 UUID 尝试 reconcile。同一容器能找到历史则完成交付；旧版本若忽略客户端 UUID 且响应丢失，仍然严格拒绝重放。返回实际 prompt ID 后持久化 submitted。

成功后依次 commit 输出字节、写不可变 produced 证据、写 ready receipt。CPU 发现 produced 而 ready 缺失时，先重新校验全部源文件，之后才补发 ready。既有 CPU 归档服务无需改协议。常规完成时不额外复制整个原件。源文件若被覆盖，下载及归档的 SHA 校验失败。

SDK 1.6.4 V2 已实际观察到禁止覆盖冲突以 InvalidError("path ... already exists") 返回，而非 SDK 源码预期的 FileExistsError；只识别对应 exact key 的此类异常，其他错误不得视作成功或已有 claim。

## 脱离原始 Runner 恢复

从 status.json 保留 durable_job_id、durable_call_id；delivery 配置另存 config.json。不需要原始 task、graph 或本地输入即可操作同一个云端尝试。

    uv run python recover_job.py status --config config.json --job JOB_UUID

    uv run python recover_job.py resume --config config.json --job JOB_UUID --call-id fc-...

    uv run python recover_job.py resume --config config.json --job JOB_UUID --endpoint https://actual-comfy-endpoint

    uv run python recover_job.py download --config config.json --job JOB_UUID --out outputs/recovered

resume 首先读取持久化证据，已完成不调用 GPU；未完成才附着已有 call 或派发同一 job ID。重复派发受同一个 claim 限制；已有 call 尚在运行时优先附着原 call。Runner 超时或退出不主动取消已 spawn 的远端 method；call ID 可继续查询。

原调用确认停止后，明确需要重跑的用户可以创建新尝试，复用旧的完整请求和输入，无需重新上传：

    uv run python recover_job.py retry --config config.json --job JOB_UUID --attempt retry-1

如果原任务是 admitted/submitted 而非明确 failed，必须加 --allow-unknown 显式承认原任务可能仍会成功、再次执行可能重复消耗 GPU。retry 只准备新 request，打印新的 UUID，不自动执行；再使用 resume 派发。相同 retry 名称幂等。若原请求尚未 claim，直接 resume 原任务；若已有 produced/completed，则先恢复结果而不是重跑。

## 不保证的行为

- 这是持久化任务身份、输入、输出和执行凭证，以及安全的 **at-most-one native POST per attempt**，不是 GPU kernel / CUDA 中间状态的任意断点续跑，也不是业务副作用 exactly-once。
- 原生进程被杀后，已 claim 但尚未产生 durable produced 的任务可能确实已经完成或部分执行。即使 Volume 上有文件，仅凭文件存在也不能证明成功，不能自动重放。
- GPU method 使用 14400s timeout（内部观察上限13800s）、retries=0；一次等待锁或长队列也可能耗尽 timeout。没有 claim 时可重派，已有 claim 时必须谨慎恢复。
- 依赖同一 Volume、同一部署版本及原生输出类型；跨部署版本或换 workspace 需要显式新身份/迁移。Volume 中的执行记录与输入需要单独设计保留期及 GC，不能随意删除 claim 或 request。
- 旧的 legacy/CPU-native Task Runner 和网页交互仍可使用，但不获得 durable 模式承诺。不要在有活动任务时滚动部署。
- 状态查询的 prepared/admitted/submitted 为持久化证据，不等同于实时 GPU 正在执行。status 在 produced 缺失 ready 时允许完成 CPU 补发；未知状态不修改旧 claim。


## 实测记录（2026-11-25）

PC2 / Modal profile zhangchenzer。所有测试使用独立 App 和独立 Output Volume，不修改生产部署或公共模型缓存。实际 GPU 测试使用 H3 BF16 套件已有 RTX PRO 6000 镜像与七文件缓存（7/7 命中，未重新下载）；工作流为 32×32 LoadImage → SaveImage，不进行 H3 采样，不比较模型性能。

- 真实 V2 并发占位：两个客户端同时 force=False 创建同一 key，owner 2 成功，owner 1 返回 InvalidError。读回确认 owner 2 内容未被覆盖。首次实验暴露异常类型差异，修复后重测通过。
- 独立 CPU Volume 集成：输入 74 bytes PNG，SHA-256 dbebd8939136c91ce197a4e7ebda5e09621777fe48caf6a0b4225004739c2919。原始本地输入删除后，四个 single-use CPU 容器分别验证首次执行、已完成任务重复调用、另一任务跨容器复用输入、注入旧 epoch claim 拒绝重放。原生引擎为测试替身；实际 SDK、Volume mount、commit、不可覆盖事件和读回验证均为真实云端操作。首次/第二任务各一次模拟 POST，完成后重放与旧 claim 各零次。测试 Volume 已删除。
- GPU 部署版本 0089ce9163f76ef79449d8a519d64719d29a5cfe148053660245ad04154ce092。初次派发后的 SDK get(timeout=0) 返回 builtins.TimeoutError，旧客户端误判为未知；已修复 pending 分支、保存 previous_call_id，原 call fc-01M88VVPNMA2XSTM34PC2BYNMF 未重新派发，重连成功。
- 实际 GPU 首次任务 b8d0bf0f-1557-5089-b75e-2f78e4a49cfc，容器 ta-01M88VZNEQF646XV3CYVS8A9MR，epoch 4b48087b3b3849aea7d43843e5204fa4。此前平台调度 Pending；GPU engine init 30.554s，原生 Prompt executed 0.11s，durable_execution 5.392s，Runner collect 11.02s。重连后的 Runner 用时 113.422s（非完整冷启动基准）。实际 PNG 451 bytes，SHA-256 d50ac88c02ddd96ee28196704cfc6deead4e120eb9a7fe16fd073532fc77e4fb，ffmpeg 全量解码后的 RGB pixel SHA-256 3a5ae9d07e34bb4268bf9f932e04aa065388db83d1499363cb0e80aca107451f，与原始 32×32 像素完全一致。
- 本地 76 项 unittest、29 项模型缓存测试、旧批量流水线集成测试通过。原始实验日志与验证文件保留于 outputs/durable-lab-1125-1600/。
- GPU 第一条任务自然归零后，client.start(first) 10.547s 返回已完成凭证且容器仍为0；recover_job.py download 独立 CLI 在本地源图片删除后下载原始 PNG，SHA-256 一致。
- 第二条任务由独立 Python 子进程连续 spawn 两个调用后退出，分别 fc-01M88W32Y8SXF6BTMM0X08Z2PA 和 fc-01M88W33MHMSP5SPMD138MC09K。第二个 GPU 容器 ta-01M88W5QCTJ1CW6XGS49F4G6RR，epoch 43c1d3409586412cbb905c52c6393a37，与第一容器不同；实际创建新 GPU snapshot（不是复用旧 snapshot）。输入源仍不存在。原生 Prompt executed 0.05s；两个 durable method 返回相同 receipt，PNG 452 bytes，SHA-256 6fa6868e61ad774a7347efe516be0ab33469da0e22d2d122c5e7345b40c6d50b，RGB 像素 SHA-256 与第一张和原输入相同。两个调用等待+验证分别 115.297s / 6.937s，不是模型推理时间。
- 故障注入 job 17b59218-16fa-590e-960d-9cfd0973a4f0 预写第一 epoch claim，在第二容器执行恢复调用返回 interrupted，云端原 claim 保留，未提交 native prompt。最终平台日志精确计数：4 次 durable method 返回（3 completed、1 interrupted），仅 2 次 got prompt、2 次 Prompt executed。两次业务执行、一次重复派发、一次旧 claim 检查，无额外原生执行。GPU 完整实验 pass；并未真实杀死正在运行的 H3 采样进程，也未测试 GPU snapshot 恢复命中、长视频/双实例吞吐及成本。
- 实验对象不进行 HF/R2 归档（此前 CPU 交付已独立验证）。本地原始证据 evidence.jsonl、cpu-evidence.jsonl、modal-runtime.log、log-summary.json、两张生成图与 CLI 恢复图保留，不纳入 Git。

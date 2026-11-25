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
| interrupted / uncertain | 单次 GPU 调用跨 epoch 或 queue/history 不可确认；不代表整个 job 已终止 | 不覆盖原 claim，不自动重放 |
| observing（客户端状态） | 已有 claim，但观察调用结束、丢失或未附着；拥有者可能仍在运行 | 只查询 Volume，最终以 completed/failed 凭证为准 |

claim 通过 Modal Volume V2 batch_upload(force=False) 原子禁止覆盖；只有实际写成功的调用可以发送一次 native POST。每个实例内长 method 串行执行；不同实例保持各自锁，commit 共享锁。容器 epoch 在 post-snapshot enter 生成，绝不被捕获到 GPU Snapshot。

当前 upstream 原生 API 可接收客户端指定 prompt_id；若丢失 HTTP 响应，使用计划 UUID 尝试 reconcile。同一容器能找到历史则完成交付；旧版本若忽略客户端 UUID 且响应丢失，仍然严格拒绝重放。返回实际 prompt ID 后持久化 submitted。

成功后依次 commit 输出字节、写不可变 produced 证据、写 ready receipt。CPU 发现 produced 而 ready 缺失时，先重新校验全部源文件，之后才补发 ready。既有 CPU 归档服务无需改协议。常规完成时不额外复制整个原件。源文件若被覆盖，下载及归档的 SHA 校验失败。

SDK 1.6.4 V2 已实际观察到禁止覆盖冲突以 InvalidError("path ... already exists") 返回，而非 SDK 源码预期的 FileExistsError；只识别对应 exact key 的此类异常，其他错误不得视作成功或已有 claim。

## 脱离原始 Runner 恢复

从 status.json 保留 durable_job_id、durable_call_id；delivery 配置另存 config.json。不需要原始 task、graph 或本地输入即可操作同一个云端尝试。

    uv run python recover_job.py status --config config.json --job JOB_UUID

    uv run python recover_job.py resume --config config.json --job JOB_UUID --call-id fc-...

    uv run python recover_job.py resume --config config.json --job JOB_UUID --endpoint https://actual-comfy-endpoint

    uv run python recover_job.py resume --config config.json --job JOB_UUID

    uv run python recover_job.py resume --config config.json --job JOB_UUID --endpoint https://actual-comfy-endpoint --reconcile

    uv run python recover_job.py download --config config.json --job JOB_UUID --out outputs/recovered

resume 首先读取持久化证据，已有 completed/failed 直接返回；只有 prepared（尚未 claim）才默认派发。已有 claim 时，指定 --call-id 可附着原调用，否则默认仅观察 Volume（即使提供 --endpoint）。重复调用的 interrupted/uncertain 或通信异常不能覆盖原任务状态；已 claim 的未决任务进入 observing，不再轮询已结束的调用，也不会额外唤醒 GPU。Runner 将 observing 和已有 call ID 写入 status.json，重启继续观察。CLI 使用 --timeout（默认3600秒）；Runner 使用整体 --timeout，若关闭整体超时，每次 ledger-only 观察仍有18000秒上限。超时只停止本地观察，不取消远端 method，不写 failed。只有显式 --reconcile 才派发同一已领取 job 来核对仍然存在的 native queue/history；依旧受 claim 约束，不会自动重放。若拥有者已消失且没有终态记录，不能仅凭观察超时宣告失败；需要操作员明确评估后使用 retry。

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

### 第二轮：真实视频、Snapshot 与双容器并发（2026-11-25）

- Snapshot ON / warmup OFF：初始两次启动均重新捕获；完整平台日志共 7 次创建、1 次明确恢复。17:45:30 恢复 capture_id fa41c9c4e7194a958400ce9f4752dadd，新的容器 epoch 实际完成两个 PNG 任务。未验证预热模型快照的稳定命中率或成本收益。
- 双容器：4 个独立 Pika H3 视频任务均完成并通过 SHA-256、FFmpeg 完整解码验证，896×512、124 帧、5.167 秒、双声道；批次派发至全部返回 149.109 秒。跨容器重复派发未产生额外原生 POST，但重复调用在原任务仍执行时返回 interrupted；该状态不能解释为原任务已失败，仍需改善客户端状态合并。
- 强制两个独立容器使用同一输出前缀，复现了相同 Volume key 被覆盖、蓝色 PNG 下载校验失败的严重缺陷。
- 修复：durable 原生提交使用派生 graph，将显式字面量 filename_prefix 放入 _durable_outputs/<job_uuid>/ 命名空间，保留原始请求、原始文件名与类别目录；成功凭证发布前校验所有产物归属。显式新尝试使用独立命名空间。动态链接、不安全路径及不符合前缀约束的输出节点不受支持；原生非 durable 模式保持原样。未知自定义输出节点绕开前缀时拒绝发布成功凭证，不保证阻止它写入任意路径。
- 修复后使用 Snapshot OFF 的隔离部署 83681a4db7e8a41b4b7da5cfc7a98dac1ddf16f5f13cb648460be42fbc81681e 验证：两种不同像素输入经独立 epoch 并发保存同名 PNG，Volume key 不同，两份 SHA-256 和 RGB 像素校验均通过。随后实际 Pika SaveVideo 输出 512×288、56 帧、2.333333 秒、双声道 MP4，完整解码通过，再读两份 PNG 仍通过。5 次原生 POST、5 次完成。80/80 unittest 通过。
- 原始脚本、证据和生成文件位于 outputs/durable-snapshot-lab-1125/ 与 outputs/durable-namespace-lab-1125/。两个隔离 App 已停止并删除各自实验 Output Volume；生产部署、公共模型缓存和生产 max_containers=1 不变。尚未验证长时间并发压力、预热快照或全部自定义输出节点，因此不默认开放多容器。


### 第三轮：预热 INT8 H3 Snapshot 与任务观察收敛（2026-11-25）

- PC2 / RTX PRO 6000 / us-east / 独立 App ap-ZPeeTmiqwf0lL2VBmR5meb / 独立 Output Volume V2 / CPU delivery / Snapshot GPU / h3-int8 warmup。部署版本 7fa45e9396c8c99fb7c4ed77c96b4edb559d52e153b22bde086c1166360df663。INT8 套件 4 个模型缓存命中，补齐 3 个权重（70,888,852,068 bytes），下载器端到端验证 246.261s。
- 首次业务容器 ta-01M8957PBF33RP8HXWR4SJP98R：预热 55.340s，engine init 68.783s，capture_id 34946c85f3634465bbe42929e0215bb3，provider unspecified，原生视频推理 2.57s，durable 6.620s，派发至返回 97.687s。首次自然归零观察 133.453s（包含列表可见性延迟，不等于 GPU 实际计费时间）；已完成 job 读取不唤醒 GPU。
- 第二次容器 ta-01M895FD8SMGBA1HMG2R0RN6JR：GCP / us-east，再次创建 snapshot，预热 45.316s，engine init 55.648s，capture_id 2409b446b7ce43abb3155513acf68ca5，原生视频 2.54s，durable 6.698s，派发至返回 86.516s。已完成并下载校验后主动停止空闲容器，非自然缩容测量；归零后读取不唤醒 GPU。
- 第三次容器 ta-01M895PCH3KVZAD1QKEPA73F7R：GCP / us-east，平台 18:58:17 明确 Restoring Function from memory snapshot，18:58:47 App Ready，复用第二次 capture_id，epoch 不同，未再次执行预热。第二次从平台 Creating 日志至 App Ready 约75s，本次 restore 至 ready 约30s；原生视频 2.87s，durable 7.917s。因平台调度等待，派发至返回反而为122.187s，不能宣称端到端冷启动改善或稳定命中率。model_ready.engine_seconds 是快照捕获时的初始化记录，不是恢复耗时。官方说明：https://modal.com/blog/truly-serverless-gpus （宿主兼容性导致同一部署可能需要多个快照）。
- 三条 Pika 视频均为512×288、56帧、2.333333秒、双声道，完整解码、SHA-256一致；第一及第三条同输入、同 seed、不同输出 prefix，MP4 哈希不同，但 FFmpeg 解码 RGB SHA-256 4a568cd90bc288367906bfe140be779a6c057ce98c7fe92037e20480742abecc 和 PCM SHA-256 190f70a17b0c1a000f0eca58335e3625a1aa2d922ae88ece86a441d437761e31 分别完全一致。此结论只适用于本次样本，不保证所有模型确定性。
- 双独立 GPU 池完成两条 LoadImage/SaveImage 初始化任务，随后拥有者执行真正的原生 H3 INT8 res_multistep 20 步视频任务 59bcc48f-cbec-56fc-b1dc-7958f1c8865e，重复调用在另一 epoch 返回 interrupted（0.576s）。客户端转换为 observing；另一个无 call ID 的进程明确禁止 _model/preflight，start 返回 observing。两个观察者分别查询8轮，与实际拥有者同一完成 receipt，GPU method 57.075s，896×512、124帧、5.167秒双声道视频 519400 bytes，SHA-256 97a05c0f065e1b2a4777068448626c115317bd321c73f27eaba2d205331b917c，完整解码成功。全程平台10次 got prompt、10次原生完成 = 4次预热+6次业务；6次 durable completed、1次 interrupted；无额外重放。两条初始化图片任务有完成凭证，本轮未单独下载验证其像素。
- 91/91 unittest 通过；核心修复提交 c8f051b19d20e6272b793f842f19e0110b66f175。两次独立观察进程均退出0。原始日志、脚本、4份视频、log-summary.json、cleanup-summary.json 保留于 outputs/durable-warmup-lab-1125/。已确认 App stopped、GPU容器0、实验Volume0。公共缓存保留，生产 max_containers=1、默认策略及实际部署均未修改。BF16 预热、长时间压力、实际计费收益、视频/音频输入及任意自定义输出节点未验证。

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
- 显式 @ 本地文件及跨步骤输出引用使用同一套持久化协议。图片：png/jpg/jpeg/webp/bmp；视频：mp4/webm/mov/mkv；音频：wav/mp3/flac/ogg/m4a/aac/opus。后缀统一小写、输入非空、字节与 SHA-256 校验，不保证所有容器/编码均可被原生解码器读取。
- 原生视频使用 LoadVideo.file，原生音频使用 LoadAudio.audio（也接受带音轨的视频）。图片保持既有能力，LoadImage.image / LoadImageMask.image 仅接受图片；未验证的自定义路径加载节点暂不支持新的视频/音频格式。新版 CPU probe 额外返回 execution_media_inputs=1，旧部署须重新部署后才能上传新媒体输入；不变更已有图片的 v1 request schema。
- 例如 set 中 `"1.file": "@inputs/clip.mp4"`、`"2.audio": "@inputs/voice.wav"`，或者 `"1.file": "$video_step.video"`、`"2.audio": "$voice_step.audio"`；也可指定 foreach job，如 $voice_step.main.audio。引用须唯一匹配对应类型；下游上传前以已收集的上游 status.json 中 SHA-256 为期望值，内容变化或缺失证据则拒绝派发。音频输出中的 MP4 作为视频引用选择，不会误判为音频文件。默认 collect=local 保留客户端收集及重新上传行为；中间步骤可选择 collect=cloud，直接使用已验证的云端产物引用（见下节）。
- 普通 UI 上传、graph 内未通过 @ 传入的历史文件名、插件远程 URL 和未适配的自定义节点不在承诺范围。旧 native 模式的 HTTP /upload/image 仍只支持图片；媒体输入使用 durable + CPU delivery。旧 batch_run.py 完整任务重启仍须计算原始 intent；原始本地文件不存在时，旧任务用 recover_job.py 按云端 job UUID 恢复已有尝试，新 workflow_run.py 计划可按工作流 UUID 恢复所有阶段。

### 云端产物直接引用（可选）

中间阶段设置 `"collect": "cloud"`，在相同 Output Volume V2、durable execution 内通过原有 $step.video / $step.audio / $step.image（或 $step.job.kind）把上游选中产物传给下游。默认 `"collect": "local"` 不改变现有下载及交付行为；末端保持 local 即可交付最终文件。终端 cloud 步骤也允许，只保存凭证，需显式用 recover_job.py download 取得文件。

- cloud 收集将 status.json 标记 completed、collection=cloud，并保存 remote_files、delivery_receipt 与 durable_job_id；files/artifacts 为空，不创建本地媒体。Runner 重启时重新向云端验证完成证据，不将空文件列表误判为丢失产物。collect=cloud 参与 intent；旧 local 任务身份不变，改变策略应使用新的 Task name 或独立输出目录。
- 下游输入 ref 增加 source={config,job_id,item}，包含精确上游部署身份及已选中产物标识；sha256/bytes/extension 与原格式一致，不创建新的 .execution/v1/inputs 对象。source 不提供任意 Volume key。stage 和 GPU materialize 分别读取上游不可变 request、produced、ready receipt，校验身份、选中范围、job 专属输出命名空间和内容证据。GPU 按 receipt 导出的原始 key 流式读取，并在 native claim 前完成原子写入和 SHA-256/长度校验；损坏、缺失或不一致一律拒绝提交。同哈希、同扩展名的本地已验证输入可复用。
- 新版 CPU probe 返回 execution_cloud_inputs=1；目标部署需支持新 ref，图片/音视频的既有 v1 对象不变。仅支持同一 Volume（可跨容器、不同工作流/部署，只要两者都使用兼容协议并且 source receipt 存在）；native 或跨 Volume 下游在构建任务时拒绝，不隐式回退。上游为 local 仍走旧的本地校验上传。
- 正常 ready 路径仅在客户端读取元数据，无中间媒体下载/重传；GPU 仍要把源内容流式校验到原生 input 目录，不是 GPU 端零拷贝。异常 produced 存在而 ready 缺失时，原有恢复流程仍会校验全部源文件再补 ready；此特殊路径可能通过客户端读回原始媒体，尚未迁移到独立 CPU worker。
- 直接引用依赖源 request、produced、ready 及实际媒体的保留；GC 必须考虑下游依赖，不能在它们仍被引用时移除源产物。旧 batch_run.py 完整任务恢复仍需原始本地输入计算 intent；单个已持久化尝试可通过 recover_job.py 按 UUID 恢复，或预先使用 workflow_run.py 持久化完整计划。当前不支持自动跨卷搬运。

## 完整工作流持久化（可选新入口）

`workflow_run.py` 适用于所有步骤均为 durable、使用同一 Output Volume V2 的多阶段 Task JSON。它与旧 batch_run.py 并存，不接管旧任务身份；不要把已经运行的旧 Task 直接当作同一个新计划续跑，以免产生新的推理。

    uv run python workflow_run.py prepare --task tasks/my-task.json --endpoints endpoints.json
    uv run python workflow_run.py status --run WORKFLOW_UUID
    uv run python workflow_run.py resume --run WORKFLOW_UUID --out outputs/my-run --timeout 1800

prepare 输出 workflow_id 和 volume，非默认 Volume 在 status/resume 追加 --volume 实际名称。保留这两个定位字段（默认卷只需 UUID）、同一 Modal 环境访问权限及兼容客户端代码。初始 Task JSON、graph JSON、endpoint 文件和本地媒体在 prepare 成功后均不再是恢复前提。resume 默认在 outputs/workflow-UUID 下重新交付，亦可选择新的本地目录。

- prepare 先展开全部 foreach 和依赖、检查图及 CPU deployment probe，解析所有 $item 及显式 @ 引用，包括未来阶段才需要的源输入；每个源对象按 SHA-256/长度/后缀校验，完成所有持久化之后才发布计划，不派发 GPU。中途失败不发布可恢复计划；已写入的内容对象可能成为无引用对象，等待后续安全 GC。
- 计划路径为 .execution/v1/workflows/UUID/plan.json；schema=1，图按内容哈希去重，覆盖字段的旧默认值移除，依赖转为明确的 job/type 选择，输入保存内容对象引用而非 @ 路径。计划 JSON 最大4MiB，身份由规范化内容及 Volume 确定；同一计划重复准备得到同一 UUID，改变参数、输入内容、任务名、模式、交付策略或部署身份会得到新计划及新尝试身份。不可覆盖发布，读取时重新校验计划哈希。计划本身不包含执行 claim，Job UUID 从计划 UUID 与各 Job 规范化身份派生，继续使用原执行协议。
- resume 从计划按依赖调度；每个 Job 的完整 request 从云端初始输入与上游已完成的选中产物重建，若已存在 request 必须完整一致。所有依赖边均使用同 Volume 直接引用，即使上游还要求 local 交付。中间步骤建议 collect=cloud、最终步骤默认 local。旧 Runner 默认行为不变。
- 完成的 Job 只恢复权威账本和凭证；local 交付可核对现有文件 SHA-256 并复用，或重新下载。若新目录没有本地状态也不重放。prepared 可以派发，已有 claim/submitted 仅 CPU 账本观察，明确失败停止；极端情况下旧 RPC 已派发但尚未写 claim，恢复可能产生额外 RPC，原子 claim 仍只允许一次原生提交。status 只读取记录，produced 且缺失 ready 时如实报告 produced，不修复；resume 使用既有恢复流程补发 ready。
- 全工作流仍需某处运行 resume 作为调度者；关闭电脑后已派发的 GPU 调用可以独立完成，但尚未派发的后续阶段不会自主启动，需在任意授权客户端继续 resume。并不是云端常驻调度服务、GPU 中间采样 checkpoint 或任意自定义节点依赖的恢复。失败的独立 retry job 不会自动替换原计划中 Job 身份，未知/失败仍需操作员处理；需要有意重新执行时创建新的计划。保留源请求、产物及计划所引用的所有记录。
- 当前只支持显式持久媒体（与原协议相同），已知原生 LoadImage/LoadImageMask/LoadVideo/LoadAudio 字段必须通过 @ 或明确上游引用绑定，隐式原生文件名拒绝；不能保证任意自定义节点内嵌路径、外部 URL 或第三方服务状态。计划所有部署的精确版本需仍可用于新派发；已完成的结果可以只靠 Volume 恢复。跨 Volume、原生/持久混合流程暂不支持。缺失 ready 的异常恢复可能仍读取客户端媒体字节。

## Runner SDK 会话复用与离线依赖感知 GC

Runner 在一次 run_task / workflow_run resume 生命周期内，以完整部署配置、端点、客户端类型为键共享 SDK 客户端，退出即释放；相同 App ID、部署生命周期 version、配置及 web URL 的已验证 CPU probe 结果和 model 句柄可复用。每次 preflight 仍查询当前 App 信息及版本函数是否存在；生命周期变化或完整验证失败不会使用旧证明。每个 durable client 的不可变 request.json 缓存最多64条/16MiB，只缓存经过验证的已发布请求（以规范化 JSON 字节隔离调用者修改）；实时 produced/failed/submitted/claim 和 ready 仍读取权威账本。活动 FunctionCall 对象复用到调用终止；轮询频率不变。刚由 start/poll 获得的完成证据可在同一 Runner 中直接交付，重新启动或仅有本地 status.json 时必须重新向云端验证。相同计划请求内同一 source descriptor 只解析一次，GPU实际媒体哈希校验不变。不增加常驻 CPU 服务。上述缓存依赖不可覆盖的账本协议，不支持绕过协议并发强制覆盖不可变记录。

离线 GC 独立入口：

    uv run python gc_storage.py plan --volume YOUR_OUTPUT_VOLUME --out gc-plan.json
    uv run python gc_storage.py apply --volume YOUR_OUTPUT_VOLUME --plan gc-plan.json --confirm-quiescent

- plan 只做只读元数据扫描，默认候选年龄至少168小时（可通过 --min-age-hours 修改）；不下载媒体内容，不自动删除。扫描所有 .execution/v1/workflows、jobs、inputs 和 .delivery/ready，识别每个计划的所有阶段（包括尚未准备的未来阶段）及所有请求（含显式 retry）的初始 CAS 引用、同卷云端产物引用、所有 produced/ready receipt 文件。校验计划哈希、请求身份、event请求哈希、source选择与凭证一致性、保留媒体存在及大小。遇未知执行schema、缺失/损坏引用、冲突事件、非预期管理对象等异常停止；未解决 claim 允许生成预览但禁止 apply。
- 当前所有计划、请求、执行事件、ready/produced及其引用内容永久保留。候选仅是未被任何根引用且超过年龄阈值的 CAS input，以及已明确写入 failed 的唯一已知 Job 私有命名空间内、未被任何凭证引用的合法媒体。未知 Job 的私有产物、成功 Job 未列出的产物、传统/native 文件、归档对象和所有账本/凭证均不会删除。不会隐式退休成功工作流或删除历史结果；未来应另设计显式保留期限/退休语义，同时永久保留防重放墓碑。
- apply 仅支持独占离线维护：必须由操作者停止针对该 Volume 的所有 Runner、GPU/CPU App、挂载、UI、归档进程和其他读写者，确保从重新扫描到删除完成没有并发访问。--confirm-quiescent 是操作者确认，并非分布式锁或自动检测所有访问者；现有旧客户端未接入在线 GC 协议。重扫对比卷名、Volume ID、全部管理对象的路径/大小/mtime、根JSON内容摘要、候选列表；预览超过1小时、扫描变化、存在未解决claim或其他校验失败均拒绝删除。逐个 exact key 非递归删除并输出已删除证据；SDK 不提供跨对象原子删除，部分失败必须重新 plan，不可直接重放旧计划。仅测试隔离卷，不对生产卷自动执行。

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

成功后依次 commit 输出字节、写不可变 produced 证据、写 ready receipt。客户端发现 produced 而 ready 暂不可见时，先等待最长15秒的凭证可见性宽限（不包含SDK请求本身的额外耗时）；仍缺失才重新校验全部源文件，之后补发 ready。既有 CPU 归档服务无需改协议。常规完成时不额外复制整个原件。源文件若被覆盖，下载及归档的 SHA 校验失败。

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

### 第四轮：视频、音频持久化与跨区域多阶段工作流（2026-11-25）

- 独立 App ap-aUlE0IJZZeYUgibtmc8sdp，独立 Volume V2，部署版本 7218f64006f8544993f62a87e201ef3a2a5a20b47af47db0f46676a7c5e0ba1f，RTX PRO 6000、CPU delivery、Snapshot OFF、warmup OFF。H3 INT8 套件 7/7 模型缓存命中；本轮执行原生媒体处理节点，不加载 H3 主模型进行采样。
- 100/100 unittest 通过（新增 9 项媒体输入回归）。原生 Runner 的三阶段 Task JSON 真实执行：load（LoadVideo/SaveVideo、LoadAudio/SaveAudio）、mux（LoadVideo/GetVideoComponents/CreateVideo/SaveVideo + LoadAudio）、extract（LoadAudio/SaveAudio，直接读取上游 MP4）。禁用客户端 native HTTP 上传及 POST，使用真实 durable SDK。测试源 MP4 24525 bytes，WAV 96078 bytes，stage 完成后删除源文件；load 后移走两份已下载中间文件（测试结束恢复），明确停止原容器并归零，再派发 mux。
- load：GCP ap-south，容器 ta-01M897VWG7GJGQ471AVS3Q5ZVR，epoch f5c20f8fe0fd43fb9f1c2c965b7d1d40，原生 0.40s；mux：GCP eu-west，容器 ta-01M89807771X33G584G7MB27XR，epoch 26fbbafa567e44f79d58f48e906d4ee1，原生 0.11s；extract：AWS us-west，容器 ta-01M898283KYK5M0TBJZ4Q32GFR，epoch e9d80f4df2ce4aba9b5c11f88b412e96，原生 0.24s。三个不同 epoch，3 次原生 POST、3 次完成；原生耗时不含调度、传输、校验。
- 四份输出均通过 FFmpeg 完整解码及 SHA-256/长度核对。load 输出 MP4 解码 RGB、PCM 哈希与原始 MP4 完全一致；输出 FLAC 解码 PCM 与原 WAV 完全一致。mux 视频 96×64、18 帧、12FPS、1.5s，主音频由 440Hz 替换为 880Hz；extract FLAC 1.504s、32kHz、单声道，频谱同为 880Hz。mux MP4 27872 bytes，SHA-256 91f8b2de24de610fafbbd3b6d3b2e4ade27ef314a82dd7f4be65f5d1715c805c；归零后仅凭 job UUID 与 delivery config 重新下载，哈希一致且未启动 GPU。
- 原始脚本、Task/Graph JSON、运行日志、4份产物及 log-summary.json 保留在 outputs/durable-media-lab-1125/；测试 App stopped、实验容器0、实验 Volume0，公共模型缓存与生产部署不变。本次实测仅覆盖 MP4、WAV、FLAC 和原生媒体节点；其他允许后缀、长视频、大文件压力、自定义节点、真实视频/音频条件生成及零拷贝跨任务传递未验证。

### 第五轮：直接云端引用、真实 H3 三阶段与故障注入（2026-11-25）

- RTX PRO 6000 独立 App ap-zJN0YzCFOXhX1vOwObnMik，版本 66d7942071ea8a98a09cd733501305b5d5bc159e8f61a7b2091d2ee22d3734e4，七个模型缓存命中。首次测试图在 Runner 预检发现断开选择器后的残余链接，未提交 GPU；修正实验图后派发，平台持续 PENDING，账本仍为 prepared、无 claim，取消未开始调用。App 停止、容器及实验卷均为0。未将等待误计为推理或声称 RTX 实测通过。
- 真实 CPU Volume V2 故障注入：App ap-XZ2VrTj4begeicrsCj3QCc，四个独立 single-use CPU 容器，原生引擎为测试替身，实际 SDK/Volume/commit/不可覆盖事件真实。WAV 16044 bytes，SHA-256 97b68990afdeaf84a9519d92701978c2916f028cc1e91147f3b94e10b47083e3；删除本地输入后首任务完成，客户端只读取元数据构造直接引用，第二容器完成相同字节。故意覆盖实验源文件后第三容器拒绝，Delivery SHA-256/size mismatch、native submit=0、job仍prepared；恢复源字节后第四容器完成同一未领取的尝试，SHA-256一致。实验 App stopped、容器0、Volume0。
- B200 独立 App ap-ydP9sjHw5P6cAGSQAwBQJP，版本 94563d5ed2f2435a582afc3837cd791db089ad3913e971813a7e1c5588c8d05a，CUDA 13.4.3 / PyTorch 2.15.0+cu134，模型7/7缓存，Snapshot OFF、warmup OFF、scaledown 30秒，首次镜像构建部署283.828s。三个实际 GPU 容器均 AWS ap-south，分别执行真实 H3 INT8 6步视频生成并保存FLAC、直接引用MP4+FLAC合成、直接引用合成MP4输出视频及提取音频。每个阶段使用不同epoch，中间阶段显式 collect=cloud；在后续派发前停止上一容器并确认归零。
- generate：容器 ta-01M89AGBVDCEDRA9QAEZV6AKWR，epoch 0af2d064b7ab42b8b89bb562c40bbcd8，job 2fed24bb-da47-5152-b4d2-6c72d8ec33ac，原生86.39s（含冷加载），durable 103.775s，输出 MP4 161671B、FLAC 36471B；mux：ta-01M89APRH7EPXMGH2W2T8ABYZR，epoch ac973a2fd48c4381aa060348c10c44b8，job 47d1d18a-8ae8-5ed8-a0a0-d5dbb59e98f9，原生1.66s、durable27.106s，MP4 135303B；final：ta-01M89ATX5427Z4GQSRD39C2VXR，epoch 1b04c609b49b48108a6364a726198067，job 8e3abf22-6761-57ef-8901-c9dd70b9bc2f，原生0.67s、durable26.960s。平台原生3次 got prompt、3次完成。原生耗时不含调度和交付。
- 实验客户端对 Volume SDK read_file 与 batch_upload 做强制审计，禁止任何中间媒体读取或上传；两份初始输入 PNG 191142B、WAV 149412B 各上传一次；中间三份媒体合计333445B，无客户端payload下载及重传，较原本各下载上传一次避免666890B本地往返媒体字节（不包含元数据/协议开销，不代表端到端加速比例）。两中间步骤只保存 workflow.json、status.json，files/artifacts为空。final 中同一 MP4 供两个节点引用。
- 最终 MP4 135450B，SHA-256 152a36fa1a91f884406fe94f6f390e6e3133c572045d05aec29504d596e683e0，512×288、56帧、24FPS、2.334秒；FLAC 35980B，SHA-256 650ac6bb42940adfee146379649dce60b50d6aea322aa0eba36fd1e22859e96a，2.336秒、32kHz、单声道。均完整FFmpeg解码、哈希及880Hz频谱校验成功。删除本地最终两文件后完整Runner重启，原始输入仍存在，仅重新下载最终171430B，两文件哈希一致，额外GPU派发0、额外上传0、中间媒体下载0。
- 108/108本地 unittest 通过；真实实验原始脚本、日志及产物保留于 outputs/durable-cloud-handoff-lab-1125/、outputs/durable-cloud-handoff-cpu-lab-1125/、outputs/durable-cloud-handoff-b200-lab-1125/。三个实验App均 stopped，容器0、实验Volume0。生产默认单容器、部署及共享模型缓存不变。当前直接引用要求 native prompt ID 与预定 job UUID 一致、同一Volume及可验证已选中产物。未实测跨App/跨部署引用、长视频大文件压力、GC保留依赖、异常ready缺失时彻底消除客户端读回、无本地原始输入的完整Task Runner重启。GPU端仍需流式读取并校验到原生input目录。

### 第六轮：完整工作流持久化、活动 Runner 退出与凭证竞态（2026-11-25）

- 独立 B200 App ap-yvplfR1Axg9hNCxCSKiWCz，版本 e4d2cfdef2d46872d5fe7188f41b29fa5175aa7daa0e315049b7eba6baeae274；同一独立 Volume V2；Snapshot OFF、warmup OFF、scaledown30s，7/7模型缓存命中，部署23.644s。新入口仅修改客户端及调度/计划协议，GPU端原v1 request及claim不变。
- 实际 CLI prepare 发布工作流27ffca4c-2506-5571-ab22-bbefcc50e57e，三个Job均not_prepared，已通过真实Volume SDK读回校验初始PNG 191142B和仅第二阶段需要的WAV 149412B。随后删除整个ephemeral-original目录（Task JSON、三份Graph、endpoints、PNG、WAV），之后所有执行/恢复只使用UUID与卷名；没有再次上传输入媒体。
- generate Job 011f34ab-6fcd-5d43-8675-7adc6ed5602f，B200 容器 ta-01M89D1SPART4W5VEEVPMGJ99R，epoch adb32a3b6e594ab2b5150f818c296347，call fc-01M89D1SH471286QZK5RPP00Q4。实验 Runner 在claim已写且状态admitted时使用os._exit(77)真实硬退出，整个lost-run目录被删除。新进程在原账本submitted期间返回observing且call_id为空，未重新派发；原H3 INT8 6步视频生成原生57.14s（含模型加载），成功完成。
- 初次观察恰遇produced已存在、ready尚不可见，旧status直接启动媒体复核，严格客户端审计拒绝中间视频读而退出；未读取该媒体、未损坏源产物、原GPU仍正常发布ready。保留resume-race.log。修复仅在客户端等待ready可见性（15s宽限，约1s轮询，SDK请求时间可能延长总耗时）；若确实缺失仍执行原有完整哈希/长度验证及不可覆盖修复。独立真实 Volume comfyui-ready-visibility-lab-1125-out 人为延迟ready发布：3次缺失读取、0次媒体读取，8.75s完成；真实缺失ready的修复26.422s成功。两项新增单测含损坏媒体拒绝；122/122本地测试通过。
- 修复后不重新生成，使用同一计划及原Job身份，从新目录恢复：generate 返回completed、call_id为空；mux Job e168b773-9e49-5c7a-a7c0-540fe57290a1，容器 ta-01M89D9QSN2VY4ETE9T44GKFFR，epoch 03cf83e934854bfd9ffbe54e4b5e0638，直接使用之前预存的WAV与上游MP4，原生1.65s；final Job 1c57a0a1-5563-5518-a79e-3454da7cd1f7，容器 ta-01M89DDS5M8FQKHHKHSMVMHCPR，epoch fb38c3ab81f64722b12f5f7951448e43，原生1.53s。三个容器均AWS ap-south、不同epoch；全程恰好3次原生got prompt/3次完成。
- 成功恢复阶段强制禁止任何客户端媒体上传及中间媒体读取，仅下载最终MP4 113024B（SHA-256 c552a03c08140fb12059b3676f0127ca27b0e9a9032fca6004f9e5d0b7292194）、FLAC 35991B（SHA-256 0870e2f25ca44b7f386b7f2c0aca279866dafb6d9d7f3a8b5c27a14614a1c14a）。FFmpeg完整解码，MP4 512×288、56帧、24FPS、2.334秒；FLAC 2.336秒、32kHz、单声道，均为880Hz主音轨。再次停止容器并删除整个recovered-run目录，只凭UUID与卷名恢复redelivery目录，最终两文件哈希完全一致、0次新GPU派发、0中间媒体读和0上传。
- 实验脚本及证据保留于outputs/workflow-recovery-lab-1125/（summary.json、ready-race-summary.json、resume-race.log、crash.log、resume.log、redeliver.log、modal-runtime.log、redelivery/）。实验App已停止，实验容器0，两实验Volume均已删除，原始共享模型缓存和生产部署未变。以上不是自动云端调度或中间采样checkpoint；调度者停止时尚未派发的阶段需新Runner继续，明确失败/无法确认claim不自动重放。跨Volume、GC依赖保留及大规模长期压力未实现/实测。

### 第七轮：Runner SDK 会话复用及离线依赖感知 GC（2026-11-25）

- 独立测试 App ap-9d9mL6IpSK7rrdmTlc96kT，版本 aa6073093a937c981c90378bb35c55be9d750fe3e94ee8f3fa02080f273bbb5a，Volume comfyui-sdk-gc-lab-1125-out。实际部署 CPU identity probe，未派发 GPU；模型缓存7/7命中。基线加载提交 afabc32 的原客户端代码，对照同一真实 SDK App 与 Volume。
- 三次同部署预检：App info 6→4、CPU probe remote 3→1、Class lookup 3→1、web URL 3→1；实测耗时15.188s→6.047s。四次 prepared 账本查询：Volume read_file 20→17、request.json读取4→1、耗时15.343s→9.422s。已完成任务查询至cloud收集：元数据读取6→3，无中间媒体下载。以上为独立样本调用链实测，不代表GPU推理或全流程固定加速比例；轮询频率未改变。
- 真实Volume V2写入26个小型测试文件（模拟执行记录与媒体，非真实GPU推理），预览识别1份计划、5份请求、1份ready、2份produced、5个保留媒体；候选3个共66B，元数据扫描未读取媒体内容。新增请求引用其中14B原孤立CAS后，旧计划重扫拒绝，删除0次；新计划精确删除31B孤立CAS及21B failed-job未发布残留，共52B，未使用递归删除。再次扫描候选0，原有保留输入、上游产物、produced未ready产物、未知及传统产物逐字节/哈希验证成功，下游materialize及缺失ready修复成功。
- 本地142/142回归通过（20项新增）；真实实验脚本、summary.json、evidence.jsonl、gc-before.json、gc-apply-plan.json、cleanup-summary.json保留于 outputs/sdk-gc-lab-1125/。实验App停止、容器0、Volume0，生产部署及生产Volume没有执行删除。GC当前为离线保守回收，不自动退休历史工作流、不保证在线并发删除安全；大规模扫描性能及大媒体压力未实测。

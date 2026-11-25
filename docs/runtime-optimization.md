# ComfyUI 性能与可靠性迁移（2026-11-25）

## 范围与决策

源项目 modal-vllm-sglang 的快照对照实验表明：缓存权重、轻量预热、真实恢复、实际业务完成时间与资源生命周期需要分别观测；不能把释放显存或端口 Ready 当成业务加速。本项目保留 ComfyUI 原生 /prompt、/history、/view、Web UI、插件和工作流，迁移经过验证的通用经验，而非另建推理引擎。

基线：4c99194c117f6d66ed36f05d43b6c99120e0f5df。modal-benchmark 已经为 0.4.3，HF 下载采用 Xet 与精确 sub_path 挂载，默认 3 Writer（可配 1–8）；本轮不重复实现、不变更依赖版本。

## 本轮变更

| 位置 | 改造 | 验证口径 |
| --- | --- | --- |
| batch_run.py | 独立提交（4）、history 轮询（8）、产物收集（4）有界线程池；同 endpoint 串行提交；依赖就绪后调度；绝对 deadline 跨线程传递 | 两 endpoint HTTP 对照、跨端点依赖与超时测试 |
| batch_run.py | 发送前保存 client_id；不确定 POST 不自动重试；下载同时算 SHA-256，原子替换，逐文件保存 collecting 状态和凭证 | 模拟响应丢失、部分下载恢复、同大小损坏；既有产物不重复生成 |
| download-models.py / comfyui.py | 已发布 HF 文件的精确 revision、size、digest 凭证（.workflow-hf-sizes）；部署预检、GPU 端链接时核对大小和版本 | 旧缓存补凭证无需下载；截断文件拒绝；保留 Xet 凭证流程 |
| runtime_policy.py / comfyui.py | 显式运行策略、真实 HTTP readiness 与进程退出检查；GPU snapshot 可关闭；捕获 ID、缓存链接、预热、恢复、退出分段日志 | 策略单测、Modal App 声明导入；未运行云端 GPU A/B |
| comfyui.py / storage/output_formats.py / storage/hf_sync.py | 停止生产进程再最终 commit；后台 commit 未结束不并发提交；保留失败日志；过滤预热、.part 产物 | 清理顺序与异常单测 |

下载 SHA-256 证明本地文件与首次收到的 HTTP 字节一致，不能替代服务端未提供的源摘要。旧 completed 状态无 artifacts 时维持原有非空/大小兼容并明确打印 legacy；新状态恢复逐文件校验。若同大小内容变更后需要修复，从已记录的 remote_files 重新下载，不自动重复提交 GPU。远端历史/临时文件已丢失时可能需要人工恢复；不保证 ComfyUI 队列在换容器后持久化。client_id 可帮助人工核对，尚未实现跨容器自动 admission reconciliation。

HF 大权重凭证主要校验身份与大小，不进行全量二次哈希；同大小损坏仍需独立审计。旧缓存首次 ensure 将访问 HF 元数据并补凭证，大小正确的权重可复用。外部 URL 文件完整性仍依赖已有 size/sha256 配置，不外推 HF 校验保证。

## 运行策略

models.json 可选 runtime 字段（或部署时环境变量覆盖）：

    "runtime": {
      "profile": "batch",
      "snapshot": "gpu",
      "scaledown_window": 60,
      "warmup": "off"
    }

COMFYUI_RUNTIME_PROFILE=interactive|batch；COMFYUI_SNAPSHOT_MODE=gpu|off；COMFYUI_SCALEDOWN_SECONDS=2..1800；COMFYUI_WARMUP_MODE=off|h3-int8。

兼容默认：交互式工作流 1800 秒，Idol 300 秒；batch profile 60 秒。保留 GPU Snapshot 默认，只有 010-minimax-h3-b200plus-suite 默认 h3-int8 预热（原有 5 帧、1 步 graph，未改模型、采样或业务画质）。不将 Pika 的 480p/5s 参数硬套到不同工作流。snapshot=off 时 engine 在 post hook 启动，warmup 独立可开关，便于受控实验。

ComfyUI 原生队列不是 Modal Function input。短缩容窗口必须结合持续的 history/WebSocket 活动、长任务、断连恢复与自然缩容验证，不能把 60 秒配置当作真实 GPU 释放时长或直接用于无人轮询的原生队列。本轮保留所有工作流默认值，未修改线上 App。

日志：engine_start → model_cache_linked → server_http_ready → warmup_completed（如启用）→ engine_initialized → snapshot_capture_ready（GPU 模式）→ post_snapshot_enter → model_ready → shutdown_begin → process_stopped → shutdown_commit / shutdown_commit_deferred → shutdown_end。用 Modal 平台 Restoring 日志、相同 capture ID 与不同容器 ID 判断真实命中，不把 post_hook_seconds 当成全部快照恢复时间。

## 本地 HTTP 真实传输对照

PC2 在 loopback ThreadingHTTPServer 上模拟两个独立 ComfyUI endpoint，各三个原生 API 任务，POST /prompt 固定 0.25 秒、GET /history 固定 0.04 秒、GET /view 固定 0.65 秒，每份传输 2 MiB，交替执行 3 轮 before/after。实际经 HTTP 传输，不使用 GPU。全部六份输出逐文件验证相同 SHA-256，各 endpoint 只收到三次提交。记录：outputs/runtime-migration-benchmark.json；可通过 testing/runtime_io_benchmark.py 重跑。

| 版本 | 第1轮 (s) | 第2轮 (s) | 第3轮 (s) | 中位数 (s) |
| --- | ---: | ---: | ---: | ---: |
| 修改前 | 5.9283 | 6.0999 | 6.0493 | 6.0493 |
| 修改后 | 1.7473 | 1.7321 | 1.6856 | 1.7321 |

中位延迟下降约 71.4%；三轮新版均有下载与其他任务轮询重叠，旧版无。该数字只代表给定网络延迟的客户端调度基准，不是实际云 GPU 加速或费用节省。

回归：主 tests 43 项、testing/test_model_cache.py 29 项、testing/test_batch_run_pipeline.py stream/batch 流程通过。无真实 GPU 新部署，不宣称本轮已验证 GPU snapshot 或 ComfyUI 60s 缩容的性能。

## 下一阶段的云端验收优先级

1. 挑选一个 H3 INT8 工作流与一个不同类型工作流（例如 Qwen Image），对同一 graph、seed、实际输出做 GPU snapshot 开/关 × 合法预热开/关，对照首次业务、连续业务、自然缩容后的重复冷启动、实际 GPU/CPU/内存费用、产物质量。
2. 以已有交互式配置为基准对照 batch 60s、120s、300s，特别验证超过缩容窗口的原生队列长任务、客户端断开与重连；选择每个工作流的策略，而非全局开启快照或统一预热。
3. 研究持久化输入、版本探针与直接 Volume 产物交付。当前 runner 图片缓存仍依赖 endpoint 的运行容器；跨容器时不能假定此前上传的临时输入一直有效。现有 HF 归档独立于推理请求，但仍在 GPU 容器后台运行；若迁移 CPU 独立归档服务，先实现已提交文件的持久队列、独占写者和失败恢复，不能仅把进程挪走或盲目降低 commit 次数。
4. 减少历史输出目录扫描与版本重复预检、避免冗余 GPU 重新部署，在真实部署数据下判断收益。不要引入未经证明的 Sleep/Wake 或强制 CUDA 缓存释放。

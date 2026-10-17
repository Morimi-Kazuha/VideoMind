# 最终独立 Review 与交付门槛

审查范围：`480b4be..governance/baseline`，以源码、Git diff、迁移前后定义对比和实际执行结果为依据。复核日期：2026-10-17。本记录补充并更新 `GOVERNANCE_IMPLEMENTATION.md` 的验收状态。

## 发现与修复

| 优先级 | 源码位置 | 事实与修复 | 验证 |
| --- | --- | --- | --- |
| P1 | `infrastructure/celery_tasks.py:70` | Celery `retry(max_retries=None)` 沿用任务默认上限，不能单独实现无限运输重试。显式设置任务 `max_retries=None`，并将 poison、异常恢复、LOCKED/RETRY 的重试发布失败统一转为 Reject(requeue=True)。业务 attempt 仍只归 TaskWorker 管理 | 真实 RabbitMQ 覆盖超过三次失败、重试发布失败、成功后停止投递及 DLQ 不被业务 worker 消费；正常请求身份保持一致 |
| P1 | `infrastructure/media/runner.py:143` | 第二次取消可能打断第一次取消的进程回收；无超时模式还会取消管道读取任务。对回收和排空进行等待保护，完成后再传播取消 | 同一重复取消测试在 `4d6172c` 原实现复现提前释放，在修复实现通过；有限/无限超时均覆盖 |
| P2 | `infrastructure/media/response_files.py:76` | 所有权锁初始化失败会留下未登记目录；删除失败会覆盖取消异常并失去重试记录。分配失败关闭句柄并回收目录；删除暂时失败保留关闭的所有者供 shutdown 重试，记录安全日志 | 锁失败目录清空；删除失败保留 CancelledError；shutdown 重试清理成功 |
| P2 | `infrastructure/r2_config.py:243` | 部分构造失败路径遗漏 MinIO 连接池，Redis close 失败会覆盖原始构造错误。各资源分别尝试关闭，保持原始错误 | MinIO clear、Redis close、engine dispose 全部调用，注入清理失败后仍返回原始配置错误 |
| 测试可靠性 | `tests/application/test_content_context_reuse.py` | 失租测试在构建开始前就注入失租，却要求构建内部 finally 执行；慢 SQL 调度可合法取消于构建前。改为构建开始后注入失租，构建等待取消 | 保留取消完成、不发布内容及释放锁的原断言；未修改生产租约逻辑 |

移除已确认无用的 DLQ 局部构造、Follow-up/SQLite 局部变量和 R2 旧凭证 import，更新音频接口过时注释。未批量移除历史公开入口或版本载荷支持。

独立修复提交：`63756e7`（运输）、`99cea1f`（资源与取消）、`4d5f18e`（失租故障注入时序）、`be7b316`（局部冗余）、`d36c888`（真实 MinIO/HTTP 及 CI）。

## 语义与结构复核

R4 迁移的 AST 对比确认：遥测 2 个定义、Provider 9 个定义、媒体 4 个定义与基线相同；变化集中在已说明的 `R4ProviderStack.close` 和媒体下载线程等待。Agent 参数兼容抽离保持旧优先级、位置参数识别及公开入口。

十轮冻结追问对比覆盖真实旧实现生成的 HTTP payload、模型调用阶段、检索请求、回答和持久记忆指纹；仅归一化时间戳。AgentLoop、Agentic RAG、Evidence Guard、M1、预算、任务及工具账本恢复均由完整既有回归继续验证。未调整 Prompt、算法、排序、预算阈值、来源规则或 feature defaults。

前端保留统一工作区代际身份；播放、会话、元数据辅助函数不引入额外状态框架。实际 Vue 挂载验证旧媒体响应、卸载、watcher 和下载行为。现有拆分有清楚职责，保留即可。

## 本地最终验证

| 范围 | 结果 |
| --- | --- |
| Python 3.12 全量回归 | **1864 passed, 47 skipped, 1 warning**，65.64 秒 |
| 真实 MySQL / Redis / RabbitMQ / MinIO / FFmpeg 专项 | **42 passed, 1 warning**，55.47 秒 |
| 原有前端测试 | **112 passed** |
| 真实 Vue 挂载 | **5 passed** |
| 前端生产构建 | 通过 |
| 生产环境契约及离线 R4 API/worker 装配 | 通过 |
| SQLite 重复迁移及模型漂移、源码编译、Ruff F821/F841 | 通过 |

曾有一次完整回归失败于上述失租测试时序，修正故障注入后重新运行全套通过；没有删除或放宽断言。新增 broker 测试初次使用非法的小于 1 秒锁重试配置，已修正测试输入，生产配置约束未改。

离线跳过项为显式 live opt-in 或工具条件。相关真实存储路径另由专项验证，不能把跳过计为通过。warning 是锁定 Starlette 对 httpx TestClient 的弃用提示。

首次远端 PR CI 的 Linux 3.12/3.13 和前端通过，基础设施作业在拉取 MinIO 时收到 Quay 401，未执行测试。官方 Docker Hub 同版本也拒绝匿名访问，发行二进制地址返回 410。因此测试 Compose 改从官方同一发行版的固定提交 `07c3a429bfed433e49018cb0f78a52145d4bedeb` 构建 MinIO；构建器使用其 [go.mod](https://github.com/minio/minio/blob/07c3a429bfed433e49018cb0f78a52145d4bedeb/go.mod) 声明的 Go 1.24.2。没有改用模拟 S3、跳过断言或改变生产 Compose。最终集成仍要求新的完整远端 CI 通过。

真实专项新增：MinIO 上传后 Redis 回执失败、并发分片、合并失锁、回执丢失后的幂等重试、内容指纹跨媒体复用；两个生产服务实例共享 Redis 播放授权，经真实 HTTP Range 读取对象；下载 MP3 经 ffprobe 确认单一音轨；无音轨返回 422，响应完成后临时目录为空。环境使用专用 `videomind-governance` Compose 项目，无业务目录挂载。

## 集成判断与剩余风险

在远端 Linux Python 3.12、3.13、前端与基础设施 CI 全部通过、PR 可合并且 main 无未审查更新的条件下，本轮没有已知阻断代码合并的缺陷。远端结果以对应 PR 和 Actions 的实际记录为准，不能用本地通过代替。

以下限制不阻断本轮代码治理合并，但阻断把本轮称为完整部署/模型能力验收：

- 未运行付费 LLM、完整 Whisper/Tesseract 真实媒体端到端、生产压测。策略未变，已完成冻结行为配对和真实存储/FFmpeg 接口验证；发布前仍按目标部署检查媒体工具、模型和凭证。
- 媒体删除仍不是跨存储原子事务；运行中任务并发写回需独立 tombstone/屏障设计，按批准范围延后。
- MySQL 与 RabbitMQ 交接仍为至少一次；发布后 ACK 丢失可能重复交接，未声称恰好一次。
- 文件删除被操作系统持续拒绝时，安全日志提示并在 shutdown 或后续启动尝试回收；不能保证权限故障期间零磁盘残留。
- 旧进程内播放凭证与 Redis grant 切换仍需排空和最长 15 分钟过期窗口，不能滚动混用后宣称链接必然兼容。

本轮不重写状态机、不合并不同资源锁、不删除历史载荷、不启用关闭的功能。局部冗余清理已经足够，建议收口，不再启动缺少收益证据的结构改造。

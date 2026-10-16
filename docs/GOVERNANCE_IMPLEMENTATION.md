# VideoMind 代码治理实施与验收记录

实施日期：2026-10-16。基线：`480b4be`。

本轮完成已批准范围内的可靠性修复、局部结构拆分、兼容依赖整理与验证设施建设。保留现有恢复、证据、记忆与预算机制，没有整体重写、新增数据库迁移或启用原本关闭的功能。

交付位于独立 worktree `D:\Agent Learning\VideoMind-governance`，本地集成分支 `governance/baseline`。各批以独立提交串联，便于顺序审查、cherry-pick 和回滚。未合并主分支、未推送、未部署。原仓库 `VideoMind-upload-finalization` 保持 `main@480b4be`，tracked diff 为空；原有未跟踪报告、图片及脚本未纳入提交。

## 1. 实施结果与行为边界

| 批次 / 优先级 | 实施内容及文件 | 验收依据 | 行为与风险 |
| --- | --- | --- | --- |
| 0 / P2 | `constraints-py312.txt`；CI 主验证 3.12、兼容 3.13；部署安装文档使用同一约束 | 先建立 Python 3.12.14 基线：1835 passed、38 skipped；前端 112 passed | 包元数据兼容范围不变；平台媒体工具及 Whisper/Torch 仍独立安装 |
| 1A / P1 | `infrastructure/celery_tasks.py` 明确 transport retry；发布失败 Reject(requeue=True)；`celery_transport.py` 默认 worker 只订阅主队列 | 真实 RabbitMQ 验证记录失败、DLQ 交接失败、重试发布失败后恢复交接 | 至少一次交接；不改变业务 attempt、队列名称及 DLQ schema。默认订阅修复避免 worker 消费 DLQ 文档；手工 `-Q` 仍应仅指定业务队列 |
| 1B / P1 | `media/response_files.py`、`api/file_response.py`、R2 服务和 API 接线 | 双实例 grant、Redis TTL、Range 206、发送异常/取消、下载线程 join、残留回收测试 | 生产凭证改 Redis 900 秒 TTL，Redis 失败关闭授权；响应独占文件在完成/取消后释放；本地模式不变 |
| 1C / P1 | R2 `ProductionMediaStore.delete_owned`：对象→向量→检查点→缓存→会话→媒体记录 | 六个阶段分别注入失败，再次删除成功；失败时身份记录保留 | 可重试清理，不是跨存储原子事务。检查点缓存额外显式清理用于把原本 best-effort 的失败纳入重试 |
| 1C / P1 | `media/audio_export.py` 与 R2 音频下载接口 | 真 FFmpeg 合成输入，ffprobe 确认仅 MP3 音轨；无音轨、转换异常、超时、取消清理测试 | 修正视频伪装 MP3 的错误行为；无音轨 422、转换失败 503；沿用原路由、鉴权及 audio/mpeg |
| 2 / P2 | `r4_telemetry.py`、`r4_providers.py`、`r4_media.py`；原 `r4_runtime.py` 保留兼容导出；worker shutdown 与基础设施部分构造失败清理 | 完整生产 factory 装配；配置失败释放 infrastructure；provider 清理失败继续释放其他资源；worker close once | 不改变每次投递的 asyncio/Celery 执行方式；生产默认 HTTP adapter 无持久连接，构造时不发网络请求 |
| 3 / P2 | `application/follow_up.py` 拆上下文、证据、生成核验阶段；`agent_compat.py` 集中原参数规范化 | 原有证据/记忆/预算测试；十轮冻结轨迹与基线一致 | 保留 `answer()` 时限与租约边界、模型顺序、提示词、候选顺序和 CAS 提交时机；工具恢复状态机不改 |
| 4 / P2 | 三个 `workspace*.js` 辅助模块；真实 Vue 挂载测试；卸载时失效工作区响应和音频下载 | 旧媒体响应、作用域卸载、实际 App watcher/Escape 清理、下载 URL 释放、卸载后迟到下载 | 代际身份仍由 `useAnalysisWorkspace` 统一管理；卸载失效属于故障修复，和纯拆分分开提交 |
| 4 / P2 | infrastructure/presentation 直接导入定义模块；旧 mode classifier 标注兼容/实验用途 | 后端完整回归与既有兼容测试 | 保留公开聚合导出、`agent_loop.py`、R2/R3 生产依赖、Demo、SQLite、评估、历史解码与回放 |

另修复静态检查确认的既有缺失名称：API 文件辅助函数的 `asyncio`、持久化类型标注的 `Mapping`。没有加入无关格式化或批量删除。

### 临时文件与取消的具体约束

- 文件位于配置媒体工作目录的 `responses/response-*` 独占目录；每个目录持有 OS 文件锁，响应期间不会被清理。
- 下载取消后等待后台写线程退出，再删除目标文件。音频转码沿用 `AsyncSubprocessRunner`，先终止并回收子进程，再释放文件。
- 文件响应禁用会延后读取的 ASGI `pathsend` 扩展，使用常规流式发送，保留 Range 支持。
- 服务启动时仅回收超过 24 小时、且成功取得所有权锁的残留目录；不依据 PID 猜测进程已死。活动目录和无法证明可回收的目录保留。
- 授权仍复查媒体所有权；删除媒体后旧 grant 不能绕过检查。Redis 不可用不降级为内存授权。

## 2. 验证结果

| 验证 | 本地最终结果 |
| --- | --- |
| Python 3.12.14 全量 `pytest -q` | **1859 passed, 43 skipped, 1 warning**，74.36 秒 |
| 独立 MySQL / Redis / RabbitMQ 与真实 FFmpeg 专项 | **26 passed, 1 deselected, 1 warning**，29.86 秒；明确排除 MinIO 专项 |
| 原有前端 Node 测试 | **112 passed** |
| Vitest + Vue Test Utils + jsdom 实际挂载 | **5 passed** |
| 前端生产构建 | 通过 |
| `compileall`、Ruff F821 | 通过 |
| SQLite `alembic upgrade head` 连续两次、`alembic check` | 通过，无模型漂移；MySQL 迁移、旧数据保留及内容并发另由真实基础设施测试覆盖 |
| `scripts/validate_deployment.py --env-file .env.example` | 通过离线 R4 API/worker 装配检查 |
| API 与默认开关冻结契约 | 与 `480b4be` 相同 |
| 十轮追问冻结轨迹 | HTTP 请求载荷、模型阶段、检索请求、回答及持久记忆摘要全部匹配基线 |

跳过项包括显式启用的基础设施、真实媒体/模型测试及环境依赖项目，不能解读为全部故障场景均已实测。唯一 warning 为当前锁定 Starlette 对 httpx TestClient 的弃用提示，不影响本轮测试结果。

`tests/fixtures/governance/api-contract.json` 从基线生成 OpenAPI 指纹及 feature defaults；`follow-up-trace.json` 使用 `git show 480b4be:src/dovideo/application/follow_up.py` 的真实实现及固定 Provider/检索输入生成。固定会话和请求 UUID，仅归一化 `created_at`，其余持久状态参与指纹比较。该轨迹覆盖十轮回答、指代改写及摘要，不代表所有功能开关组合；其他路由、反思、修复、预算组合由原有测试覆盖。

本地日志保留在 worktree 中且被 Git 忽略：`work-final-backend.log`、`work-live-final.log`、`work-node.log`、`work-components.log`、`work-build.log`、`work-deployment.log`。没有将日志或秘密配置提交。

### 可重复执行

主验证环境使用 Python 3.12：

```bash
python -m pip install -c constraints-py312.txt -e ".[test]"
python -m pytest -q
python -m compileall -q src scripts alembic
python scripts/validate_deployment.py --env-file .env.example
cd client
npm ci
npm test
npm run test:components
npm run build
```

基础设施验证使用专用 `docker-compose.governance.yml`，项目名 `videomind-governance`，只绑定本机端口 23306/26379/25672；测试数据库、Redis keys 和 broker topology 使用独立身份，禁止指向生产连接。安装 FFmpeg/ffprobe 后，按照 `.github/workflows/ci.yml` 的 infrastructure job 设置测试连接及显式 opt-in，再执行该 job 列出的六个测试文件。Windows 可用 `DOVIDEO_FFMPEG`、`DOVIDEO_FFPROBE` 指向工具文件。

测试结束执行 `docker compose -f docker-compose.governance.yml down -v`。本轮已完成清理，原有 `dovideo-r2` 容器未操作。

## 3. 保留与延后

保留任务/内容身份隔离、任务锁/内容构建锁/上传锁、失租保护、死信交接状态、结果恢复、检查点与历史执行记录分离、来源证据与记忆分离、分层预算、媒体取消收尾、生产严格失败和前端请求代际隔离。

以下变更继续按原计划延后：

- 删除与运行中任务的完整隔离：需要 tombstone、写入屏障与持久清理流程；当前仍可能发生删除后并发任务重新写入衍生数据，不能宣传原子删除。
- AgentLoop 工具状态机重写、统一所有锁/预算、合并历史与检查点：现有结构承载恢复和去重责任，本轮没有相称收益支持改变。
- 历史载荷/公开入口删除：缺少线上数据与外部使用证据。
- 检索算法、Prompt、排序、预算阈值及默认开关调整：属于独立质量策略变更。
- 全量前端状态库迁移：保留当前代际身份即可支持本轮拆分。

没有进行付费模型调用、真实 ASR/OCR 端到端运行、MinIO 故障专项、线上压测或实际多 API 部署。共享 Redis grant 已用两个独立实例及真实 Redis 验证。新增 Linux CI 和 Python 3.13 作业尚未在远端运行；不得把配置完成当成远端通过。

## 4. 提交、审查、发布与回滚

依赖顺序如下，每批可单独审查；后续集成应依次合入，不能跳过依赖：

| 提交 | 目的 |
| --- | --- |
| `a95cac3` | 固定验证基线及依赖 |
| `6b0c6f5` | poison handoff 重试、DLQ 默认订阅修复及真实 broker 验证 |
| `0a7f34f` | 共享播放凭证与响应文件生命周期 |
| `a1c032e` | 可重试删除顺序 |
| `f0dab4f` | 真正 MP3 导出 |
| `1e34077` | 缺失名称修复 |
| `fc8d774` | R4 职责机械迁移与兼容导出 |
| `5fcf8eb` | worker 与部分初始化资源清理 |
| `bdf1de8` | 追问阶段及 Agent 参数兼容边界 |
| `68a82d4` | 前端三个内部职责拆分 |
| `ab6210b` | 卸载失效修复与真实组件测试 |
| `34b0b53` | 内部直接导入及旧分类器定位 |
| `e73c401` | 重试发布失败、音频异常及取消验收 |
| `665ce01` | 冻结契约、独立基础设施 CI 及部署约束说明 |

最终文档提交只补充本记录与文件尾部空行整理。没有把原仓库未跟踪文件混入任何提交。

发布前需在隔离目标环境通过新增 CI，并保持现有 feature flags。检查正常业务投递、运输重试次数、DLQ 积压、临时目录占用、删除重试率、追问失败类别和预算终止指标。仍需覆盖目标环境 MinIO 与实际媒体处理；这些是发布门槛，不是本轮已经通过的结果。

播放凭证切换必须排空旧 API 请求，等待旧内存 grant 最多 15 分钟后切换全部实例，避免新旧实例混用。回滚 Redis grant 版本同样先排空并使在途链接过期。Redis grant 会自然 TTL 到期，无需迁移删除。

纯结构调整可按逆依赖顺序 `git revert <commit>`；媒体、音频和运输修复也以独立提交回滚，但会恢复对应已知缺陷。回滚需连同依赖的新文件/测试批一起审查，不要强行回滚前置基础提交而保留依赖代码。本轮无数据库字段或持久载荷版本变化，不需要数据降级。未来 tombstone 或历史格式变更必须另行设计兼容迁移，不能套用本轮代码回滚结论。

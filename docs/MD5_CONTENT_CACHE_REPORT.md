# VideoMind MD5 内容缓存改造报告

实现与验证日期：2026-10-21（Asia/Shanghai）。2026-10-22 用户授权清点并提交推送，初次交付时的“不自动 Commit/Push”限制已由本次明确授权解除。

## 1. 源码与版本核对

实际修改目录：`D:\Agent Learning\VideoMind-governance`，开始时工作区干净，HEAD 为 `350fa48b836550ee6014e742fb5ad80f7aabbb71`。GitHub 最新合并提交为 [058d8ef](https://github.com/Morimi-Kazuha/VideoMind/commit/058d8ef87c92d720e71ac42cfaee585e1ec9dd92)；[远端比较](https://github.com/Morimi-Kazuha/VideoMind/compare/350fa48b836550ee6014e742fb5ad80f7aabbb71...058d8ef87c92d720e71ac42cfaee585e1ec9dd92)显示仅多一个合并提交、文件差异为空。较旧的 `dovideo-python` 检出缺少需求中的模块且已有未提交工作，本轮没有修改它。

已阅读实际上传、媒体导入、R4、数据库模型/Alembic、Checkpoint、Worker、证据与执行记录代码。上传合并和普通导入均由服务端增量计算完整文件 MD5，持久化至 `MediaRecord.content_hash`，`to_ref()` 已可传递该值。

原项目参考使用远端 [df4fa83](https://github.com/Xiaoc7r/DOVideo-AI/commit/df4fa83a78a51dfa1718606bf47cf49942e1112e) 的 `AnalysisTaskKeys.java`、`AiService.java`、`VideoAnalysisConsumer.java` 和 `ChunkUploadService.java`，未依据过时的本地 Java 镜像推断。本轮借鉴内容复用与任务隔离，不采用其跨媒体最终答案复用。

## 2. 原设计与新设计

| 项目 | 原设计 | 新设计 |
| --- | --- | --- |
| 内容身份 | 视频完整 SHA256 + 动态 PipelineContract，再计算结构化摘要 | 32 位小写 MD5；Key 为 `md5:<MD5>` |
| 查询前的成本 | 每次下载完整 MinIO 视频并扫描文件 | 读取可信媒体记录，先查询缓存 |
| 缓存未命中 | 下载后处理 | 在内容锁和双重检查后下载，边下载边计算 MD5，核对记录后处理 |
| 历史缺失/无效 MD5 | 无直接优化 | 下载时计算 MD5，进入同一缓存及锁；不批量回填 |
| Agent 身份 | `(mediaId, goal, mode)` | 保持原语义 |
| 共享范围 | 中立 VideoContext、ASR/OCR 原始观察及其溯源 | 保持原范围，无最终 AgentState/AnalysisResult 复制 |
| 动态版本 | 环境变量及多项运行配置参与 Key | 删除；固定预处理，变更策略时显式清理重建 |

## 3. 修改文件与关键函数

路径均相对于上述实际修改目录。

| 文件 | 变更 |
| --- | --- |
| `src/dovideo/application/content_context.py` | `ContentContextKey` 仅接收 MD5，验证格式并统一小写；保留原 `ContentContextPreparation` 锁、续租、双重检查及降级 |
| `src/dovideo/infrastructure/content_context.py` | 删除 `pipeline_contract()` 和 `sha256_file()`；保留 `RedisContentBuildLock` |
| `src/dovideo/infrastructure/r4_media.py` | `build_context()` 重读媒体记录、核对来源、延迟下载；`_download()` 在下载流中计算 MD5；`_object_coordinates()` 同时约束命中路径 |
| `src/dovideo/infrastructure/persistence/content_context.py` | `SqlAlchemyContentArtifacts` 为旧非空列使用固定兼容值，继续完整校验 payload 与原始观察 |
| `src/dovideo/infrastructure/persistence/sqlalchemy.py` | `_media_value()` 将历史空白指纹转换为缺失值，允许可靠回退；不改表结构 |
| `src/dovideo/infrastructure/r4_runtime.py` | 清理已删除函数的兼容导入 |
| `.env.example`、`scripts/validate_deployment.py` | 移除动态流水线缓存版本配置与校验入口 |
| `tests/application/test_content_context_reuse.py` | MD5、旧缓存、内容差异与原并发/故障回归 |
| `tests/infrastructure/test_r4_content_reuse.py` | 真实 R4 调用边界的缓存、来源、任务隔离、历史回退、取消与证据回归 |
| `tests/infrastructure/test_engineering_live.py` | 改用真实 MD5，验证无 MinIO 对象时的缓存命中 |
| `tests/infrastructure/test_deployment_validation.py` | 移除已删除配置项的旧测试参数，保留其他生产配置断言 |
| `docs/BACKEND_ENGINEERING.md` | 同步实际流程与兼容、部署边界 |
| 本报告 | 交付说明 |

`value_objects.py`、`analysis_task_keys.py`、`worker.py`、`checkpoint_service.py`、`execution_record.py`、Evidence Guard、领域 provenance、上传和导入源码均未修改，已通过 Git diff 核对。

## 4. 命中、未命中及信任边界

1. API 原有 `require_owned` 继续授权提交。Worker 在原任务锁和生命周期内进入 R4。
2. R4 按 mediaId 从数据库重读记录，要求其 source 与接受的请求一致，并校验 MinIO bucket/object；缺失、来源变化或非法来源直接拒绝。消息中的 `content_hash`、filename 不决定缓存身份。
3. 有效持久化 MD5 直接生成内容 Key。命中时仍由 MySQL 确认持久化记录/摘要，并从原始 ASR/OCR Observation 重建校验证据；返回时绑定当前合法 source 和 goal。
4. 未命中则获取现有 Redis 内容锁并再次查询。确需构建时才下载，增量计算 MD5，若与持久化值不一致则拒绝预处理和发布。随后运行原 ASR/OCR、构建中立 VideoContext，完整成功才共享发布。
5. 历史 MD5 为 None、空白、非十六进制或旧 SHA256 时，先下载得到真实 MD5，再走同一套查询及内容锁；没有第二套 SHA256 新缓存。此路径每次仍可能下载，本轮未引入持久化回填机制。
6. 命中、异常、取消均保留文件清理；下载取消先等待后台写入线程结束，关闭响应并归还连接，再清理文件。

**可信 MD5 命中明确跳过完整 MinIO 下载、文件哈希、ffprobe、FFmpeg 和 ASR/OCR。** 内容 payload/provenance 的 SHA256 校验仍存在，这是必要的证据与完整性用途，不是重复扫描视频。

## 5. 旧缓存、数据库和旧证据

- 没有新增 DDL 或迁移。既有 `cache_key VARCHAR(64)` 可容纳 `md5:` 前缀及 32 位 MD5，`fingerprint VARCHAR(64)` 可存 MD5；非空 `pipeline_contract` 仅写固定值 `fixed-md5-preprocessing`，不再动态参与身份。
- 旧 SHA256 Key 无法匹配新命名空间，无需迁移或删除；旧 Redis 提示按 TTL 过期，旧数据库缓存行保持闲置，需清理时另行明确操作。没有删除用户视频、媒体记录、任务执行历史。
- MD5 身份使 SourceRevision 与旧设计不同。当前媒体上下文重建时，原边界继续清空不同 revision 的 chunks；原索引查询按 SourceRevision/chunkId 隔离，旧索引不能混入新检索结果。
- 新旧证据混用会被拒绝。既有 Execution Record 的不可变 header 继续拒绝使用新 SourceRevision 恢复旧未完成执行；本轮没有修改或伪造历史。已完成任务仍按原 TaskKey 幂等返回其历史结果。
- 上线切换前须停止接收/排空旧 Worker，清点旧未完成执行。若存量记录 revision 冲突，需要明确的历史处理方案，不能宣称可自动无损续跑。本轮未对真实业务记录进行这种操作。

## 6. 测试命令与真实结果

工作目录：`D:\Agent Learning\VideoMind-governance`。Windows 沙箱会阻塞 asyncio 初始化所需的回环 socket pair，因此以下成功运行均在允许本机回环通信的环境执行；未修改异步机制来绕过测试。

```powershell
.venv/Scripts/python.exe -u -m pytest -q --tb=short -p no:cacheprovider --basetemp='D:/Agent Learning/tmp/md5-full-final'
```

**1884 passed, 47 skipped, 1 warning，68.43 秒。** 跳过项主要需要各自显式开启的实时依赖测试；另有音频导出用例因 FFmpeg 未在当前进程可用而跳过。Starlette/httpx 弃用警告与本轮无关，未计作失败。

```powershell
.venv/Scripts/python.exe -u scripts/run_engineering_live_tests.py --env-file 'D:/Agent Learning/dovideo-python/.env.r2.local' --include-upload
```

**17 passed, 1 warning，20.06 秒。** 实际连接 MySQL/Redis/MinIO，覆盖独立测试库的迁移/数据保留、内容并发和续租、旧 token 隔离、持久化及提示失效、MinIO 命中和既有上传链路；测试资源由现有脚本及 fixture 清理。未对业务库运行迁移。

核心针对性回归在补充空白 MD5、取消及历史执行守卫后为 **56 passed，14.41 秒**；之后增加的 3 个非法持久化来源参数用例也已纳入最终全量通过结果。

```powershell
git diff --check
```

通过，无空白错误。原有 TaskKey、Worker、Checkpoint、Evidence、上传/导入核心文件的 diff 检查为空。

覆盖需求：跨 mediaId 相同内容仅一次下载/预处理；不同目标及 mode 分别执行 Agent 并保存四份独立结果/Execution Record；命中禁止下载；同 MD5 并发、租约丢失和缓存故障；SourceRevision/segmentId/sourceItemId 与错误来源拒绝；旧 SHA256 缓存、旧上下文和无效 MD5；伪造消息哈希、非法来源、下载 MD5 不一致与取消清理。

真实 MinIO 测试在首次构建后删除两个视频对象，第二个媒体仍能成功命中，直接验证命中不访问对象。真实依赖测试中的 ASR/OCR 构建使用确定性观察替身，本轮没有执行真实 Whisper/Tesseract 或付费 LLM 端到端分析，不能将其写作真实模型 PASS。

排障记录：首轮全量出现未改动的 `test_failure_persists_durable_stage_and_safe_cache_metadata` TTL 精度边界（604800.0000000001 > 604800），单独复跑也曾失败，最终全量未重现。其测试、内存缓存和 Checkpoint 实现均未修改，未放宽断言。第一次真实依赖运行选用的 r4 配置缺少测试库管理员凭据，随后改用现有 r2 配置并完成验证。

## 7. 第二遍审查与风险

已从审查者角度复查：新路径只有 MD5 内容身份；PipelineContract 生成器和文件 SHA256 扫描已删除；旧 Key 无法进入新路径；消息不能自行指定可信哈希；缓存仅存中立预处理；TaskKey、锁/重试/恢复、最终结果与历史语义均保留。原 SHA256 证据、来源、帧引用及 payload digest 未误删。

保留的技术边界：

- MD5 不具备抗恶意碰撞保证。即使 MD5 来自服务端计算，不同文件仍可能碰撞；Evidence Guard 不能证明其不可能。本轮遵循指定的单 MD5 复用方案，不额外引入双哈希。
- 快速命中依赖持久化指纹与 MinIO 内容一致、上传后的对象保持不可变。绕过上传链路修改对象或数据库可能产生过期命中；未命中下载可发现不一致，命中不会重新读取字节验证。
- 固定预处理配置变更必须显式排空、清理、重建缓存及相关上下文，不能靠环境变量自动切换 Key。
- 历史缺失/无效 MD5 未自动回填；旧持久化缓存也无新增 GC，因此仍有下载/存储成本。
- 存量未完成旧 Execution Record 不会自动跨 revision 恢复，需上线前处理；既有 TTL 精度波动仍需后续独立修复。真实 ASR/OCR/LLM 端到端未在本轮运行。

## 8. 完成与封板判断

**本轮代码改造、要求的核心回归、真实基础设施验证及复核完成，可以进行源码验收。** 最终全量无失败；后续按用户授权提交到独立功能分支，Git 历史记录实际提交状态。

**不宣称无条件生产封板。** 上线前应确认固定流水线及对象不可变约束，完成旧未完成执行的切换清点，并接受/另行处理上述 MD5、TTL 波动与真实模型验证边界。没有擅自迁移业务数据或覆盖执行历史。

### 2026-10-22 推送前只读清点

推送前重新运行内容缓存、R4 集成边界和生产装配测试：**59 passed，17.12 秒**；暂存区空白校验及常见凭据模式检查通过。

清点范围为现有本机 r2 配置指向的数据库，使用只读事务，没有修改或删除记录；不代表其他环境或生产实例的状态。

| 检查项 | 数量 |
| --- | ---: |
| 媒体记录 | 47 |
| 缺失/空白 MD5 | 1 |
| 非空但格式无效的 MD5 | 0 |
| Execution Record：COMPLETED / FAILED / STARTED | 21 / 14 / 0 |
| 内容缓存：旧身份 / MD5 身份 | 2 / 0 |
| Lifecycle 阶段：COMPLETED / DEAD_LETTERED / BUDGET_EXHAUSTED / CONSUMING | 62 / 25 / 8 / 5 |
| 媒体上下文 Checkpoint | 92 |

1 条缺失 MD5 可使用本轮下载回退；2 条旧缓存自动无法命中新 Key，不必删除。STARTED 执行记录为 0，但还有 5 条 CONSUMING 生命周期快照，不能据此声称没有旧未完成任务；这些快照与 14 条失败执行如需重试，须先核对原请求、队列和执行历史。它们不阻止推送源码，但仍限制直接切换部署。本次只做清点，未重试、清理或迁移这些业务状态。

## 9. 面试用 60 秒技术回答

VideoMind 借鉴了原项目的两层去重思路：内容层复用，任务层隔离。上传时，服务端增量计算完整视频 MD5，存进媒体记录。分析时先经过媒体授权，再从数据库重读可信 MD5 和来源，用 `md5:<指纹>` 查询共享 VideoContext。命中就跳过视频下载和 ASR/OCR；未命中才在 Redis 内容锁下双重检查，下载并核对 MD5，然后构建并持久化预处理产物。缓存里不保存用户目标、私有路径或 Agent 历史，返回时重新绑定当前媒体 Source 和 goal。Agent 任务继续用 mediaId、goal、mode 作为 TaskKey，因此相同视频能共享预处理，但不同媒体、目标和模式都有独立的 Checkpoint、结果及 Execution Record。SourceRevision 和 Evidence Guard 继续校验证据，旧 revision 不会静默混入。MD5 只是内容复用指纹，不是授权凭证，也没有抗碰撞保证。

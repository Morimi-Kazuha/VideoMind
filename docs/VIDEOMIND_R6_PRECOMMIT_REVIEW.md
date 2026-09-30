# VideoMind R6 Pre-Commit Review

Review date: 2026-09-30。审查对象是已有 R6 工作树及本轮必要加固，不改变产品定位。

## 1. Status

**PASS — R6 code and real-browser acceptance gates passed.**

P1-LIVE-01 **RESOLVED**；P1-LIVE-02 **RESOLVED：新 revision 继承旧 request 用量的计量缺陷已修复**。
真实 media 40：V1/V2 COMPLETED，Follow-up HTTP 200 / 4 verified citations，Scenario A/B/C PASS，
V2 Browser Terminal Handling PASS。**P0 genuine open=0，P1 genuine open=0**。
全量 **921 backend / 62 frontend PASS**；build / compileall / diff-check PASS。
预算仍为 50,000 tokens / 240,000 ms / 2 rounds；无预算扩容、verifier 放宽或模拟响应。
多轮 Follow-up memory 仍有意 DEFERRED。以下旧 NOT READY/COMPLETED-only gate 文本是历史快照，
由本报告末尾的最终收口证据取代；保留原 500、422 和 budget failure 记录。
Git 收口按 commit → fetch → rebase → 全量复验 → fetch → fast-forward push 执行，最终 SHA 与同步结果见交付报告。

## 2. Reviewed Scope

- 入场：32 modified + 11 new = 43 files；先完成逐文件 diff / 新文件 / 测试质量审查，后修改代码。
- 前一轮快照：**33 modified + 12 new = 45 files**。新增修改范围是 revision checkpoint request binding；新增文档是本报告。
- 本轮最终范围：**35 modified + 12 new = 47 files**；tracked +1367 / -299；新增 P1 支持集 resolver、production API regression；未改 dependencies / lockfiles / brand / deployment。
- HEAD: `6eaaa6111527dd43fb7cee4cb9b60b330492d076`。
- 执行并检查 `git status`、完整 diff、`--stat`、`--numstat`、`--check`；新文件另行读取，不能仅靠 `git diff` 覆盖。

下列路径均相对于仓库根目录；M 为已跟踪修改，N 为新文件。每个文件都已读实现或测试并核对用途。

| M 文件 | 审查范围 |
|---|---|
| `client/src/AnalysisWorkspace.vue` | 子请求 generation / session、失败转录、延迟 seek |
| `client/src/App.vue` | list/auth/upload ownership、跨 tab、finally、重复上传 |
| `client/src/api.js` | session generation、401 及响应体解析边界 |
| `client/src/api.test.js` | 同 token 再登录、旧 401、跨 tab / logout races |
| `client/src/chunkUpload.js` | scoped resume、legacy migration、lost complete response |
| `client/src/markdown.js` | token traversal、skip stack、sanitize 顺序 |
| `client/src/markdown.test.js` | 嵌套 prose、code/link/escape/raw HTML 跳过 |
| `client/src/taskEvents.js` | stream identity、abort、reader 清理、terminal |
| `client/src/useAnalysisWorkspace.js` | operation ownership、rerun、metadata、playback、后台完成 |
| `src/dovideo/application/checkpoint_service.py` | staged revision request binding / partial apply |
| `src/dovideo/application/dispatch.py` | reserve → stage → queued → enqueue / compensation |
| `src/dovideo/application/errors.py` | typed 415 domain error |
| `src/dovideo/application/follow_up.py` | deadline；P1 构造前解析 verified refs、保留引用级 count metrics |
| `src/dovideo/application/evidence.py` | 原 verifier 不变；新增 quote/time/revision/segment 支持集解析 |
| `tests/presentation/test_p3_grounded_follow_up_api.py` | 12 refs 正常 200，无有效来源走 grounding 422 |
| `src/dovideo/application/media.py` | 扩展名错误类型，不改变媒体身份 |
| `src/dovideo/application/status.py` | active revision / orphan / dispatch failure precedence |
| `src/dovideo/application/status_projection.py` | 更大 attempt、terminal 不被迟到事件覆盖 |
| `src/dovideo/application/task_lifecycle.py` | 事件携带既有 request_id |
| `src/dovideo/application/worker.py` | stale delivery、revision retry、context goal、execution record |
| `src/dovideo/infrastructure/celery_runtime.py` | request_id publication / STALE ack |
| `src/dovideo/infrastructure/media/uploads.py` | 400 / 409 语义，marker / cleanup 顺序 |
| `src/dovideo/infrastructure/persistence/repository.py` | hydration 仅写回 payload，不倒退当前 stage |
| `src/dovideo/infrastructure/providers/follow_up.py` | 单次 adapter 调用、30s 窗口、typed failure |
| `src/dovideo/infrastructure/providers/model.py` | remaining deadline / 4096 output bound / provider retries |
| `src/dovideo/presentation/api/app.py` | domain HTTP 映射、missing file 400 |
| `src/dovideo/presentation/api/r2_runtime.py` | 上传异常一次转换，消息脱敏 |
| `src/dovideo/presentation/api/r3_runtime.py` | durable subscribe、revision dispatch |
| `src/dovideo/presentation/api/r4_runtime.py` | 执行 request_id、durable SSE、真实 usage telemetry |
| `src/dovideo/presentation/api/runtime.py` | local revision / trace / transcription / subscriber / upload TTL |
| `tests/application/test_p3_grounded_follow_up.py` | 实际 service 的 checkpoint / retrieval / model deadline |
| `tests/infrastructure/test_celery_r3.py` | 延迟旧 worker delivery |
| `tests/infrastructure/test_checkpoint_repository_8a.py` | hydration 与明确 stage transition 分离 |
| `tests/infrastructure/test_p3_grounded_follow_up_provider.py` | 真实 provider adapter budget / retries |
| `tests/infrastructure/test_uploads_3c.py` | owner、lost response、同一 mediaId、late chunk |

| N 文件 | 审查范围 |
|---|---|
| `client/src/chunkUpload.test.js` | 实际 uploader + storage / fetch ports |
| `client/src/r6App.test.js` | 实际 App script actions + Vue refs |
| `client/src/r6TaskEvents.test.js` | 实际连接池 / reader 故障注入 |
| `client/src/r6Workspace.test.js` | 实际 composable、延迟响应、实际组件 seek 函数 |
| `client/src/uiStage.js` | presentation-only phase map |
| `client/src/uiStage.test.js` | 未知、terminal、prototype 名称阶段 |
| `docs/VIDEOMIND_R6_ASYNC_RELIABILITY_REPORT.md` | 第一轮报告 + 本轮 addendum，保留历史快照 |
| `docs/VIDEOMIND_R6_AUDIT.md` | 第一轮修改前审查矩阵 |
| `docs/VIDEOMIND_R6_PRECOMMIT_REVIEW.md` | 本轮结论与最终 gate |
| `src/dovideo/application/durable_task_events.py` | lifecycle / status 一致 snapshot + history filtering |
| `tests/application/test_r6_async_reliability.py` | 实际 dispatch / checkpoint / worker / subscribe / status |
| `tests/presentation/test_r6_upload_api.py` | 真正 HTTP routes + local store concurrency / TTL |

## 3. Findings

原 pre-commit 将同一根因的多个边界归为一个 finding；H1-H5 / M / L 已修复。
历史 **P1-LIVE-01（原 OPEN，现 RESOLVED）**：candidate refs 9–12 被误用于 bounded verified evidence，ValidationError → 500。最终修复见 §19。当前 OPEN 为 P1-LIVE-02：真实 V2 预算失败，未完成最终浏览器 gates；不将风险改为 ACCEPTED。

### Critical

无 P0 finding。

### High

| ID | 问题及修复 | 状态 / 回归证据 |
|---|---|---|
| H1 | staged revision 未绑定 request_id；部分写入后清理失败可能被旧执行消费，apply 失败又在 retry 之外。绑定 request_id、保留 legacy None 兼容、apply 纳入有限 worker retry | FIXED；partial-stage+failed-cleanup、before/after-apply、duplicate delivery |
| H2 | SSE snapshot 仅比较 request_id，合法 retry 的新 attempt 可能混入旧 terminal。比较完整 lifecycle，先应用当前 snapshot，再过滤 history | FIXED；status read 中切 attempt、旧 attempt terminal、新 attempt completion |
| H3 | 共享 VideoContext 的 user_goal 可能属于另一目标或为空。worker 对当前 TaskKey goal 绑定不可变副本 | FIXED；retry 目标正确、共享 checkpoint / 原 context 未改写、stale delivery 不释放 active |
| H4 | terminal callback 等待 metadata，plan 请求不返回时 UI / stream 无法结束。即时提交 terminal，metadata 独立捕获失败 | FIXED；pending metadata 不阻塞 terminal / release |
| H5 | orphan revision 在无 active 时可能因旧 result 被标成新 COMPLETED。nonterminal revision + inactive 显示 FAILED 可重试，保留旧 result | FIXED；orphan revision、QUEUED / PROCESSING precedence、dispatch-failed old result |

### Medium

| ID | 问题及修复 | 状态 / 回归证据 |
|---|---|---|
| M1 | 跨 tab token change 的 reset 删除共享新 user metadata。仅重置当前 tab 操作，并读取新 user | FIXED；实际 App storage event + 新列表 |
| M2 | 同媒体分析 generation 作废 pending playback，URL / loading 被遗留。播放请求使用 media view / session / 独立 request ownership | FIXED；同媒体分析、媒体切换后的 old finally |
| M3 | 第二次 upload click 作废第一次已启动上传。提交期间重复动作无操作 | FIXED；首个 controller 保留、只 init 一次 |
| M4 | undefined / null / 字符串 userId 可生成共享 resume namespace。公开 helpers 校验正安全整数 | FIXED；无 storage 写入 / fetch / 共享 key |
| M5 | workspace 关闭后过滤掉后台 completion，丢失既有通知和列表刷新。允许同 session 的后台完成，重新打开同 key 仍拒绝旧 callback | FIXED；closed completion + reopened stale stream |
| M6 | cleanup 同步异常 / pending cancel / terminal handler 异常可能掩盖原错误或重连，迟到 HTTP error body 可误通知。best-effort 非阻塞清理、terminal finally release、body 后再查 ownership | FIXED；四个实际 reader / pool 故障回归 |
| M7 | 失败 retranscription 隐藏旧文本。存储 last success，active 优先；失败显示旧文本和新 FAILED / error | FIXED；actual local worker + initial SSE + workspace UI state |
| M8 | local completed / abandoned upload 不回收；到期 status 可能移除合并中数据。活动入口清理过期记录、完成即释放 chunks、保留 in-flight merge 并刷新完成 TTL | FIXED；TTL / bytes cleanup、并发完成、合并期间到期仍恢复同 mediaId |
| M9 | 旧播放器 loadedmetadata callback 调用 seek 时可能跳转新播放器。校验 session、sidebar identity / generation / visible、原 player | FIXED；实际 SFC seek 函数正向跳转 + player / workspace replacement |
| M10 | checkpoint / retrieval / model 原生 TimeoutError 被当成普通失败。传播到统一 timeout 分类 | FIXED；三个边界分类 + retrieval / model 真正 cancellation |

### Low

| ID | 问题及修复 | 状态 |
|---|---|---|
| L1 | usage record 数量误称 actual provider calls；缺失 tokens 被记录成 0。改为 usage_records / reported_total_tokens，缺失用 None | FIXED；provider budget / usage 现有测试及实现核对 |
| L2 | phase map 的 inherited property 名称会返回对象或函数。使用 Object.hasOwn，未知保持 last phase | FIXED；constructor / __proto__ regression |

- **ACCEPTED**：24h 完成恢复窗口、既有跨存储补偿窗口和 SSE 无 cursor 的交付边界（见 16）。此前不可达依赖 note 已解决；P1-LIVE-01 未接受。
- **DEFERRED**：多轮 grounded follow-up memory；本轮明确不实现。

## 4. Auth Session Review

`api.js` 将 token 与 sessionGeneration 一起捕获。login、logout、replacement、storage event（含 clear）
均使旧 session 失效；显式同值 token 再登录也失效。set / clear 先写 storage 后 force sync，没有漏增；
storage 事件可额外失效，generation 不承担连续编号语义。旧 401 在清 token / 发 auth-expired 前验证身份，
body await 后再次验证。当前 401 仅触发一次失效；旧成功 body 同样不可回写。

App list/auth/upload 的旧 finally 验证 operation identity；upload progress 还验证 session / owner / controller。
跨 tab 重置当前 tab，不删除新 tab 写入的 user。API token 仍是授权来源，不使用 user metadata 授权。

## 5. Workspace Generation Review

analysis generation 表示本次工作区结果操作的写权限，覆盖 analysis / rerun / revision / follow-up /
feedback / evidence / metadata / history / close / reopen / media switch / logout。每个 await 后再查 ownership；
identity 包含 sidebar / generation / media / goal / mode / auth。相同 media+goal+mode 的新 revision 也能隔离旧响应。

重复 rerun 在创建新 generation 前检查 rerunLoading，第一次已受理提交响应仍可启用对应 SSE。
播放属于媒体视图，使用独立播放 request 序号，分析目标改变不会丢失同媒体 pending URL。
demo timers 捕获 current；延迟 seek 也需原 player 权限。后台完成仅允许同 auth session 通知 / 刷列表，
不能写进重新打开的同 key workspace。

## 6. SSE Review

前端 map 删除先核对同 controller；abort 清理订阅和 retry timer。reader cancel 的同步异常、拒绝或
pending Promise 不阻塞 terminal；releaseLock 异常不替换原 read 错误。terminal handler 即使抛异常也
release，随后不能重连；同一 buffer 的后续 PROCESSING 不覆盖 terminal。永久 HTTP 错误终止；可恢复错误保留退避。

后端 request_id 是执行身份，attempt 是同一执行的合法重试号，stage 仅用于投影；TaskKey 未增加新字段。
consistent snapshot 比较完整 lifecycle，status read 或 history read 中发生 retry / transition 就重新取快照。
当前 attempt snapshot 先于旧 history 进入 projection，防止旧 terminal 结束新 attempt。
每个 subscribe 获取当前 durable 状态；没有新增 cursor 或 exactly-once 承诺。local initial status 失败也在 finally 删除 subscriber。

## 7. Backend Task / Revision Review

TaskKey 仍为 **mediaId + goal + mode**；content_hash / revision / request_id 不参与锁或历史 key。
相同内容的不同 media 有独立 active / result；同媒体的新 goal 使用副本，不改写共享上下文来源。

| dispatch 场景 | 审查结果 |
|---|---|
| A reserve 后 stage 失败 | 尝试撤销 staged plan，始终尝试 release；即使 stage 实际写入且 cleanup 失败，foreign request_id 不能消费 |
| B stage 成功 enqueue 失败 | 明确 DISPATCH_FAILED、撤销 stage、释放 active，旧成功结果仍可读；无永久 QUEUED |
| C enqueue 成功 HTTP response lost | 同 TaskKey active reserve 返回 DUPLICATE，不覆盖第一次 lifecycle / plan；重复 worker delivery 幂等 |

worker 拒绝旧 request_id 并 ack STALE，不释放新 active。应用 plan 的 before/after-write 错误进入原有限 retry；
已应用 marker 支持重试；FAILED revision 不能恢复旧 result 为该执行新成功。active revision 细分 QUEUED /
PROCESSING；orphan FAILED；dispatch failure 可恢复旧成功结果。新 revision 建立独立 execution record，旧历史保持可读取。

## 8. Durable State Review

repository hydration 只把缺失的 durable payload 写回 hot cache，不顺便写回历史 stage；明确 write / save_stage
仍完整更新 payload / stage，合法 retry transition 没有被简单的 stage 排名拒绝。load_plan / load_result /
load_critic 等读不会把当前 CRITIC stage 退回 PLANNER。status_query 不要求 hydration 完成才能投影当前 lifecycle。

新增 context goal 绑定不改变 source_revision、segment_id、source_item_ids、evidence_frames、chunks 或证据来源。
同媒体 provenance 保持；跨媒体无 final result 复用。新 revision / 合法 retry 的 execution record regression 已通过。

## 9. Upload Review

公开 resume key 为正整数 userId + file identity。无有效 owner 时立即拒绝，不创建共享 key；旧用户 pending 上传
不能写凭据、进度或 loading。legacy key 只供迁移读取，必须先调用服务端 owner status；403 或网络失败保留 legacy。
scoped storage 写成功后才删 legacy，写失败仍保留；临时 5xx 不错误清除可恢复会话。

生产 merge 受原锁保护；先写 completed marker，再 best-effort 清 chunks / session。marker 写失败执行现有 record /
object rollback；lost response 后 status / complete 返回同 mediaId。local 完成立即清 chunk bytes；完成元数据
从完成时保留 24h，后续活动入口清过期 values / markers。合并中的会话不被 TTL/status eviction 移除；并发 complete
返回 409，成功后重试恢复同 mediaId。local 仍是开发内存 adapter，不承诺重启持久化。

HTTP 映射已审查：400 malformed / missing multipart，401 未登录，403 wrong owner，404 missing / expired，
409 incomplete / merging / completed late chunks，413 oversize，415 unsupported extension，503 storage unavailable。
presentation 单次转换 domain error；typed status 保留且不泄漏私有存储信息。

## 10. Markdown Security Review

实际流程为 **marked lexer → token traversal → marked parser → 原 sanitizer**；没有把 token stringify 回 markdown 再全局替换。
只转换 prose text，包括 bold / list / table 内的时间戳。code、codespan、link、image、escape 与 raw HTML
`a/code/pre` 内容跳过；`**[01:23]**` 可转换，link 内 codespan 保持字面量。
HTML 时间链接在 sanitizer 前产生。真实 Edge DOM 验证 script / img / handlers / javascript href 被剥离，
code / raw anchor 字面量保留，long-minute / hour link 正确。原 verified citation / timeline 来源契约不改变。

## 11. Follow-up Budget Review

service 外层 60s asyncio timeout 与 context-local budget 覆盖 checkpoint、prior result、retrieval、model 及 verification。
adapter 使用不延长父 deadline 的最多 30s model window；provider 每次 timeout 取 remaining，FOLLOW_UP output 上限 4096。
service / adapter 各只调用一次，不提供外层 retry 或 repair；provider 使用既有配置 max_attempts（默认 3）。
因此默认最多 **3 次** HTTP attempts，非 2×3。既有 transient 3 次、timeout 1 次、4xx 1 次测试通过。

新回归在 retrieval / model 到达边界时 reschedule 真实 asyncio timeout，验证底层 await 被取消、无 checkpoint writes，
不用 sleep 制造 race。native TimeoutError 保持 timeout 分类。预算约束的是 async 请求响应路径；同步 CPU 工作或已启动的
to_thread I/O 不能被 asyncio 强制杀死，其完成仍受底层客户端 timeout 管理，不宣称 OS 层硬实时取消。

telemetry 只聚合已报告 usage；usage_records 不伪称调用次数，缺失 tokens 用 None。grounding / EvidenceVerification 保留；
未新增聊天历史、全局 memory cache 或未经验证的 sources。

## 12. Test Quality Review

Backend 本轮 **+15**（887 → 902），Frontend **+13**（49 → 62）。新增回归使用实际实现，fakes 位于外部 ports。

| 测试 | 是否覆盖真实路径 / 限制 |
|---|---|
| r6Workspace | 调用实际 useAnalysisWorkspace，deferred fetch / JSON 和事件顺序控制 late response；seek 从实际 SFC source 读取执行，没有复制实现 |
| r6App | 实际 App script 提取并注入真实 Vue refs/computed、API/uploader；storage/upload/list actions 真运行；watch/lifecycle 为 harness stub，不冒充 DOM integration |
| r6TaskEvents | 实际 pool / consumeStream，pending fetch/read/cancel、同步 cleanup throw、terminal callback throw、replacement error body |
| chunkUpload | 实际 uploader / public helpers；fake fetch/storage 控制 owner、403、network、quota 和 lost response |
| uiStage | 真实 presentation map；未知、terminal 和 inherited property 名称保持 phase |
| test_r6_async_reliability | 实际 dispatcher、SQLite checkpoint + hot cache、worker、status query、ProductionR4 subscribe / local worker；端口故障注入 |
| test_r6_upload_api | FastAPI routes + auth + domain exception 映射；首次 complete response 丢弃后确认同 mediaId / 仅一条记录；actual local store TTL / 并发 merge |
| follow-up tests | 真实 service timeout，controlled cancellation；真实 provider adapter、max attempts / remaining timeout / output limit |

未发现复制核心逻辑、mock 掉被测主体、恒真断言或仅 call-count 冒充结果语义。新增 race 使用 deferred Promise、
Future、Event 和可控调度；没有 arbitrary sleep / setTimeout。1ms deadline 测试等待永不完成 Future，验证真实取消，
不依赖两个请求的随机先后。Node 测试不是 mounted Vue E2E；另执行真实 Edge demo mount + DOM sanitizer smoke。

全量既有回归覆盖 Evidence Verification、Execution Record、Historical Replay、Tool Calling / Recovery、Failed Task Replay、
Jev Routing / Route Recovery、Token Budget、Rate Limit、VideoContext、Citation → Timeline。
本轮受影响区域另有 revision history、retry identity、provenance / context goal、timestamp seek 的针对性回归。

## 13. Unrelated Diff

逐文件确认全部属于 R6 reliability / regression / documentation；无需 revert 无关更改。
没有依赖升级、lockfile 修改、大面积格式化、generated/build/coverage/cache/log、local config 或 credential 文件进入候选 diff。
`work/` 检查脚本、JSON，`client/dist`、node_modules、Python cache 均 ignored，不是待提交文件。
Git LF→CRLF 提示源于仓库既有 autocrlf；正常 diff / numstat 无全文件换行 churn，未修改 Git 配置。

静态搜索覆盖所有修改/新文件：TODO、FIXME、HACK、XXX、console.log、print(、debugger、sleep(、time.sleep、setTimeout。
无临时 TODO/debug 输出；timer/sleep 命中分别是提示、auth success 切换、object URL revoke、demo、upload backoff、
SSE reconnect/poll、既有 R3 restart fault harness，均已核对 ownership / cancellation 或原演练用途。
token/api_key/secret/password 命中是 runtime 字段、环境配置名称、测试 fixture 和报告说明；逐段核对无真实凭据。
私钥头及常见高风险 key 格式扫描为 0；这与人工核对一起构成凭据 gate，不把 regex 扫描声称为完整 secret detector。
脱敏 inventory / scan 证据在 ignored `work/r6-precommit-hygiene.json`。

## 14. Historical Validation (before P1 patch)

| Check | 最终结果 |
|---|---|
| Backend `.venv/Scripts/python.exe -m pytest -q` | **902 passed**，1 个既有 Starlette/httpx deprecation warning |
| Frontend `npm test` | **62 passed**，0 failed / skipped / cancelled |
| Build `npm run build` | **PASS**，Vite production build |
| Compile `python -m compileall -q src` | **PASS**，项目 Python 3.12.14 |
| Diff `git diff --check` | **PASS**，另核对所有新文件 whitespace |
| Other | **PASS**，headless Edge demo / DOM smoke 6/6；静态 diff / secret hygiene / inventory |

没有升级依赖来消除已有 warning。新增测试夹具的共享 context 预期、UploadSession import 和有效时间区间错误均已修正后
重新全量通过；没有放宽生产断言或用 skip 绕过失败。

## 15. Historical Live Validation (500 retained)

**FAILED — LIVE VALIDATION FAILED**（后续 live 补验更新）。

五项协议与 worker 当前 PASS；media 37 的基础真实分析 / citation / timeline / Scenario B/C / revision 通过。
真实 follow-up 返回 HTTP 500，成功 Scenario A 和 V2 browser terminal 未完成；不是依赖不可用。
详细 request / execution IDs、counts、原因和证据见 Async Reliability Report §18。
以下表格保留为 **live 补验之前的 pre-commit 历史预检**，不代表当前服务状态。

自动验证通过后，单次读取已有批准的 local environment、使用现有 R4 representative media 和配置执行有限预检。
每个 dependency socket timeout 1s；不打印 endpoint / credential，不反复连接。结果来自当前执行环境，
不能据此区分远端服务停机与网络隔离，也未验证认证。

| Dependency | 实际结果 |
|---|---|
| MySQL | TimeoutError（TCP connect timeout） |
| Redis | TimeoutError（TCP connect timeout） |
| MinIO | TimeoutError（TCP connect timeout） |
| Qdrant | TimeoutError（TCP connect timeout） |
| RabbitMQ / broker | TimeoutError（TCP connect timeout） |
| Celery worker | NOT CHECKED：broker 不可达，无法执行 inspect ping；不能声称 worker 本身超时 |
| Representative media | 现有 `work/media/representative-long.mp4` 存在 |

实际 preflight 证据：ignored `work/r6-live-preflight.json`；未启动 live media workflow、未破坏任何生产数据。
upload / analysis / live SSE / citation / timeline seek / follow-up / revision 都 **未做真实媒体 live 验收**。
Scenario A follow-up late after rerun、B close/reopen processing SSE、C lost complete response 在确定性 regression 中覆盖；
这些 unit / TestClient / demo 结果不冒充 live PASS。

## 16. Remaining Risks

- P1-LIVE-01 已解决；当前 V2 BUDGET_EXHAUSTED 阻止成功 Scenario A 和浏览器 COMPLETED 验收。多进程重启恢复不属于本次已执行的 scenarios。
- MySQL / Redis / MinIO / broker 没有共享事务。已审查 request binding、补偿、锁和 TTL，但同时发生多存储写入及 rollback 故障的既有窗口仍存在；不承诺 exactly-once。
- 幂等上传恢复限于 completed marker 24h TTL、相应 media 仍存在；local 内存 adapter 不跨重启持久化，到期数据在后续活动时清理。
- SSE 恢复当前状态并过滤同 execution / attempt 的旧消息，不保证每个中间事件只出现一次；legacy None request_id 保持兼容。
- asyncio 取消无法杀死已运行的同步 CPU / thread I/O；response path deadline 有真实取消验证，底层 I/O 仍需客户端 timeout。
- multi-turn follow-up memory 按本轮要求延期；当前保持单次 source-grounded answer，非跨 worker 会话记忆。

当前 OPEN 为 **P1-LIVE-02**，P1-LIVE-01 已解决；最新证据见 §19。客户端取消不撤销服务端已受理任务，属于现有异步契约。

## 17. Commit Recommendation

**NOT READY**。自动验证、diff / hygiene 和真实 Follow-up PASS，但 Scenario A 和 V2 browser
COMPLETED gate 未满足，必须停止 commit/push。未增加预算、绕过 verifier 或改生产 AgentLoop。

## 18. Git

- Branch: `main`。
- HEAD: `6eaaa6111527dd43fb7cee4cb9b60b330492d076`（未改变）。
- Working tree: **35 modified + 12 new = 47 files**，全部未提交。
- Commit created: **NO**。
- Push performed: **NO**。
- Deploy performed: **NO**。
- Release / tag changed: **NO**。
- Real credentials changed: **NO**。

## 19. P1 Final Diff Audit and Failed Gate

本轮必要生产变更仅 application/evidence.py、application/follow_up.py、infrastructure/providers/follow_up.py，
对应回归在原 service/provider/API 测试文件。完整现有 R6 diff 和新增文件再次审查：auth/generation/SSE、
revision dispatch/worker、checkpoint hydration、upload、Markdown、deadline 及测试均属于原 R6 范围。
本轮未修改 SSE / upload / workspace generation 代码，既有 B/C regression 仍全部 PASS；新媒体额外 live B/C PASS。

Resolver 保留 domain bound=8、非空 provenance、原候选 membership、正确 revision/segment/time/source
和 digest/quote 完整匹配。稳定最小支持跨度、正确 8/3 refs 保留、12/9 实际支持 refs、重复去重、无效集拒绝、
超限拒绝、ASR+OCR 和 OCR frame/identity 均有回归。后续仍使用原 verifier，失败保持 422。
额外 prompt observation mapping 最多 16 条 / 400 字符，不改变 evidence invariant，不接受模型返回来源 ID。

最终 backend **919 passed**（本轮 +17）、frontend **62 passed**、build / compileall / diff-check PASS。
真实 media 39 Follow-up **HTTP 200**；5 candidates；3 citations；refs **[11,11,11] → [1,1,1]**；
rate limit ALLOWED；usage 2 records / 3455 reported tokens；60s/30s/4096 路径有效，无外层 repair/retry。
成功响应仅由 harness 暂缓转发，随后真实 V2 revision HTTP 202，generation 改变且旧历史 active 阶段不变。
V2 最终 **FAILED / BUDGET_EXHAUSTED**，attempt=2、worker DEAD_LETTERED；失败任务 33，
BudgetExceededError。未完成 successful late-A delivery 和浏览器 V2 COMPLETED，不能宣称 Scenario A PASS。
详见 Async Reliability Report §18 finalization；**P1-LIVE-02 OPEN（acceptance blocker）**。

47 文件无 .env、work、logs、screenshots、node_modules、dist 或 pycache 候选路径。高风险 credential
格式命中 0，新文件 whitespace 0；password/token 值为 tests fixtures / 参数 / 配置名称 / 文档说明，
未发现真实秘密。timers 为上传退避、SSE heartbeat/reconnect 或有 ownership 的交互；没有新增 debug 输出，
没有依赖升级、无关格式化或测试 hack。结论来自扫描加人工审查，不宣称完备 secret detection。

**Overall NOT READY**：不扩大范围修改 AgentLoop / Planner / Executor / Critic，不增大 budget 迁就 live。
Follow-up Memory 有意 DEFERRED，不是 blocker。保持工作树供后续接管；本轮不 commit、不 push、不 release、不 deploy。


## 20. Final Closure Diff Audit — PASS

历史 §17–19 的 NOT READY 和 COMPLETED-only gate 由本节取代；所有旧失败证据保留。
P1-LIVE-01 RESOLVED；P1-LIVE-02 RESOLVED，具体预算取证、对照和真实 browser 验收见 Async Report §24–27。
新 revision 错误继承 V1 17,594 tokens，导致 Critic 的 22,130-token admission 在只剩 19,149 时失败。
修复独立 request trace，同时保留同 request retry 用量、旧 trace；去除 API enqueue 后 trace reset 竞态。
不修改预算、retry 分类、Planner/Executor/Critic 或 verifier。现有预算错误直接不可重试的测试通过。

本轮新增审查文件：infrastructure/r4_runtime.py（锁内接受请求后划分预算）、
infrastructure/redis_observability.py（request trace identity）、
tests/infrastructure/test_executor_response_observability.py（2 项真实 boundary regression）；
presentation/api/r4_runtime.py 移除 enqueue 后 reset，其余已有 R6 改动保留。
当前总审查范围 38 modified + 12 new = 50 files；tracked +1457/-306。新增文件全部已读实现或测试，
完整已有 diff 按 auth/session、generation、SSE、revision、checkpoint、upload、Markdown、grounding 复核。
没有 dependency/lockfile、品牌、部署、README 或无关范围变更。

54 targeted / 921 backend full / 62 frontend PASS；build/compileall/diff-check PASS。
真实 media 40 Follow-up HTTP200，5 candidates，4 citations，refs 11→1，原 verifier PASS；
usage 2 records / 3736 tokens；rate ALLOWED。
真实 V2 attempt1 COMPLETED / Critic PASS；浏览器真实 SSE COMPLETED，loading=false,error empty。
generation4 的真实成功 A 原样释放至 generation5 后，完整 workspace hash 相同、5 citations 不变、
manual evidenceResults0 不变、timeline/plan/metadata 不变、所有 stream snapshot 不变。
B/C 新 media live PASS；旧 V1 execution hash 保持不变。P0/P1 genuine open 均 0。

静态 scan 和人工检查：secret literal 0，新文件 whitespace0；仅 source/tests/docs 可提交。
env、password/token 会话数据、work/、screenshots/live JSON/temporary harness/logs/build/node_modules/pycache
均未纳入。Ignored 证据不会移到 source tree。多轮 memory 有意 DEFERRED，不是阻塞项。

Phase0 fetch 已确认原本地 HEAD6eaaa61、origin/main f37e814、落后3个README提交；
保留06fcc7c/004933e/f37e814，以 clean R6 commit rebase，不操作 dirty pull/reset/checkout，禁止 force push。
commit 后和 push 前再次 fetch，post-rebase 全量通过才允许普通 push main。
此为提交准备阶段报告；最终Git/GitHub验证、SHA和clean状态在交付报告记录。
Release/tag/deploy/package publish NO。

Staged diff 最后检查发现新 durable_task_events.py 的 EOF 多余空行，已移除；不改变运行逻辑。

# Analysis TaskLock lease renewal final report — 2026-10-06

## 1. Status

PASS WITH NOTES. READY TO PUSH after the focused local commit. No push is
performed in this round. Notes: Windows solo worker crash/redelivery was tested;
Linux prefork child-loss and Redis failover were not exercised. There is no
exactly-once or database fencing claim.

## 2. Baseline

- Checkout: isolated `VideoMind-upload-finalization`, branch `main`.
- HEAD before: `803b93b3c297642814837d16ada6a860ba2143e6`, equal to remote main.
- HEAD after: the focused commit containing this report; obtain with
  `git rev-parse HEAD` (reported explicitly in the delivery response).
- Initial tree clean. Final source/doc/test changes belong only to this task.
  The original `dovideo-python` tree's 19 tracked modifications and 11 untracked
  entries remain untouched. No upload/frontend/prompt/AgentLoop changes.

## 3. Current-source Audit

The [pre-implementation audit](TASK_LEASE_SOURCE_AUDIT.md) records the actual
HTTP dispatch -> RabbitMQ -> Celery -> production R4 runtime -> TaskWorker ->
TaskLock -> AgentLoop -> checkpoint/history/result/lifecycle -> ACK/retry path.

Before this change, RedisTaskLock used random URL-safe owner tokens and
`lock:analysis:{media_id}:{goal_digest(goal, mode)}`. SET NX PX acquired a
900,000ms lease. Existing Lua atomically checked the token before release or
refresh. TaskLockPort exposed acquire/release only; TaskWorker acquired before
recovery/attempt work and released in finally, with **no periodic refresh**.
Completion order was result -> lifecycle -> completion marker. Pending durable
dead-letter handoffs were recovered before terminal shortcuts. Active markers
were retained for retry/stale/handoff recovery. The AgentLoop also wrote final
result checkpoints, so a guard only at Worker.save_result would be incomplete.

Celery had late ACK=true, reject-on-worker-lost=true, failure/timeout ACK=false,
prefetch=1. Both RETRY and LOCKED retried after 0.25 seconds with unlimited
transport retries; persisted business attempts remained bounded by TaskWorker.

Upload remains separate: redis-py MergeLock with 30-minute finite lease,
20-minute merge deadline and boundary refresh. Its code and Lua are unchanged.

## 4. Root Reliability Gap

At t=0 A acquired token A and entered AgentLoop. At t=900 seconds Redis expired
the fixed lease while A was still running. Duplicate delivery B then acquired
token B and entered AgentLoop for the same task. Both could write checkpoint
and final result state. Owner-safe release alone did not close this window.

## 5. Lease Renewal Design

TaskLockPort now exposes `lease_seconds` and `refresh(key, token) -> bool`.
Redis reports its actual configured TTL in seconds; the default stays **900s**.
Only explicit process-local nonexpiring locks report None; production has no
missing-capability fallback. Local lock refresh/release also check token identity.

TaskLeaseKeeper renews every TTL/3 (**300s default**) using the existing Lua.
Acquire and refresh start times, measured with monotonic time, define conservative
validity deadlines, accounting for network latency. Each refresh await is bounded
by the smaller of TTL/3 and remaining known validity. A transient exception is
logged without provider secrets and retried after at most TTL/6, within that
deadline. A successful refresh extends known validity. A false result raises
TaskLeaseLost; exhaustion raises TaskLeaseExpired. Unexpected keeper errors are
also surfaced through TaskLeaseUnavailable rather than abandoned in the background.

Loss cancels the cooperative delivery child task. Even if an Agent/provider
swallows cancellation or blocks the event loop, local deadline/ownership guards
reject subsequent persistence. Cancellation is joined; no event loop is killed.
Crash terminates renewal and Redis eventually expires the finite lease. Release
uses the unchanged owner-safe Lua; an old token cannot renew/delete a new lease.

## 6. Worker Integration

After acquire, handle runs the existing locked body through a scoped keeper.
Result/lifecycle/completion ordering, stale request IDs, bounded attempts,
permanent error classification, staged revisions, execution history, saved-result
recovery and durable pending DLQ handoff ordering remain intact.

A ContextVar carries only the current application lease to child tasks and
to_thread. Checks at worker mutation boundaries and the two central checkpoint/
execution-record repository bridges cover AgentLoop-owned result/history writes
without changing AgentLoop logic. Outside delivery these checks are no-ops.
Known lease loss bypasses business retry/failure writes: it preserves the active
reservation, releases only its token safely, and raises for transport recovery.
External cancellation also preserves the recovery reservation. Ordinary RETRY
continues to refresh/preserve active markers; terminal success/failure releases
them unless a durable handoff remains pending. Finally cancels and joins renewal
before lock release for every exit, including stale/recovery/early exceptions.
LOCKED creates no keeper.

Production R4/R3 composition injects the existing TaskWorker automatically; no
extra opt-in is needed. Tests instantiate the R4 factory and demonstrate renewal
and duplicate exclusion. The canonical Celery default remains R4. Live smoke
explicitly selects deterministic R3 to avoid paid provider/media dependencies.

## 7. Broker Interaction

COMPLETED, STALE and durably DEAD_LETTERED outcomes return for ACK. RETRY uses
the existing business delay and unlimited transport retries; TaskWorker owns the
bounded business counter. LOCKED now uses **5s** by default through
`DOVIDEO_CELERY_LOCKED_COUNTDOWN_SECONDS` (finite, minimum 1s). It is a delayed
retry, not an ACK/discard, and consumes no analysis attempt. This reduces default
contention retry frequency by 20x. Lease errors use existing recovery retry.

Worker process death -> renewal stops -> Redis TTL expires -> unacknowledged
RabbitMQ delivery reaches a new worker -> it acquires, resumes lifecycle and
checkpoint state. A result committed before ACK loss is recovered instead of
re-running AgentLoop. A pending DLQ publication is resumed instead of skipped.
The live harness validated all these paths and drained its generated main queue.

Late ACK alone does not imply child-loss redelivery; reject-on-worker-lost is
also required. See [Celery task semantics](https://docs.celeryq.dev/en/stable/userguide/tasks.html#acks-late)
and [configuration](https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-reject-on-worker-lost).
Disabling failure ACK is not a universal immediate-requeue policy; explicit
adapter retry handles worker recovery failures. Windows solo whole-process loss
was tested; Linux prefork child termination is NOT RUN.

## 8. Fault-window Validation

| Case | Evidence | Result |
| --- | --- | --- |
| A | 0.3s lease, four renewals while Agent blocks; second Worker excluded. Real Redis 0.85s work; real broker 3s lease held beyond 4.2s. | PASS |
| B | Keeper stopped without release; finite Redis/fake TTL expires and replacement acquires. Broker worker forcibly killed and restarted. | PASS |
| C | Real Lua rejects token A after token B acquired; B's 5s TTL remains above 4s instead of being reset to 0.3s. | PASS |
| D | Old-token release leaves B's token and TTL intact. | PASS |
| E | Exact result -> lifecycle -> marker order, keeper joined before release, no further refresh or asyncio task leaks. | PASS |
| F | Retry stops keeper/releases lock, preserves/refreshes active, next attempt completes; live three-attempt transient proof. | PASS |
| G | Exhausted attempt and pending handoff survive failed broker publication; next delivery finishes DLQ without another Agent run. Existing durable ledger/DLQ and live permanent-failure proof retained. | PASS |
| H | False refresh cancels work; even cancellation-swallowing Agent cannot save result/completion/failure. Public checkpoint and history writes reject known loss. | PASS |
| I | Brief exception recovers; sustained outage/hung await expire conservatively with recovery marker retained. No unhandled coroutine warnings. | PASS |
| J | Duplicate cannot enter AgentLoop; Celery LOCKED delayed retry, no business increment; live Redis/process duplicate exclusion. | PASS |

Additional checks: event-loop stall, slow acquisition, invalid lease contracts,
external cancellation, stale delivery, unexpected early read failure, default
15-minute Redis lease, configurable LOCKED delay, and default R4 DI.

## 9. Regression

Final exact-source results:

- Full backend: **964 passed, 12 opt-in integration skips**, one existing
  Starlette/httpx deprecation warning. RuntimeWarning promoted to errors.
- Focused lease + Celery: **37 passed**, with RuntimeWarning promoted to errors.
- Real Redis lease integration: **3 passed**.
- Real RabbitMQ/MySQL/Redis smoke: **PASS**, seven named behavior proofs plus
  durable topology/drained queue, Celery 5.6.3, Windows solo pool.
- Upload Redis/MinIO/MySQL regression: **9 passed** (independent module unchanged).
- Frontend production build: **PASS**. No frontend source change.
- Python compileall and git diff whitespace check: **PASS**.
- Dedicated lint/type: **NOT RUN**, no configured repository commands.
- Linux prefork child-loss, Redis failover and paid ASR/OCR/LLM execution:
  **NOT RUN**, beyond this bounded coordination test.

Run from a configured checkout using its Python environment:

```powershell
python -m pytest -q -W error::RuntimeWarning
python -m pytest -q tests/application/test_task_lease.py tests/infrastructure/test_celery_r3.py -W error::RuntimeWarning
python scripts/run_task_lease_live_tests.py --env-file <ignored-infrastructure-env>
python scripts/run_upload_live_tests.py --env-file <ignored-infrastructure-env>
python -m compileall -q src scripts/run_task_lease_live_tests.py
git diff --check
# client directory: npm run build
```

The 12 full-suite skips are the 3 lease and 9 upload opt-in tests, each run
separately against live infrastructure. No failure is relabeled as a skip.

## 10. Files Changed

| File | Reason |
| --- | --- |
| `.env.example` | Document separate contention retry delay. |
| `application/ports/tasks.py` | Provider-neutral duration/refresh contract. |
| `application/task_lease.py` | Small scoped keeper, expiry/loss signals, persistence guard. |
| `application/worker.py` | Renewal lifecycle and owner-valid write/recovery boundaries. |
| `application/checkpoint_service.py` | Guard Agent-owned checkpoint I/O centrally. |
| `application/execution_record.py` | Guard durable history I/O, preserve lease error classification. |
| `infrastructure/redis.py` | Expose TaskLock's existing TTL; existing lock/upload Lua unchanged. |
| `infrastructure/celery_transport.py` | Configure validated LOCKED delay. |
| `infrastructure/celery_tasks.py` | Map LOCKED to its own delay; leave RETRY/recovery policy intact. |
| `presentation/api/runtime.py` | Explicit local lock contract and owner identity checks. |
| `tests/application/test_task_lease.py` | Fault-window, cancellation, persistence guard and R4 DI tests. |
| `tests/application/test_task_worker_9b.py` | Migrate existing fake to the explicit local lease contract. |
| `tests/application/test_dead_letter_handoff_9b_fix2.py` | Same fake migration; preserve DLQ regressions. |
| `tests/application/test_r6_async_reliability.py` | Same fake migration; preserve revision/recovery regressions. |
| `tests/infrastructure/test_celery_r3.py` | ACK/configuration, delay, lease-loss recovery, R4 selection tests. |
| `tests/infrastructure/test_task_lease_redis_live.py` | Actual Lua ownership/TTL and renewal/crash tests. |
| `scripts/run_task_lease_live_tests.py` | Explicit live credential loading and isolated deterministic broker proof. |
| `docs/TASK_LEASE_SOURCE_AUDIT.md` | Pre-implementation CURRENT/TARGET/GAP. |
| `docs/TASK_LEASE_REPORT.md` | Verified design, behavior, evidence and remaining limits. |
| `docs/ARCHITECTURE.md` | Current analysis lease architecture. |
| `docs/INTERVIEW_GUIDE.md` | Evidence-backed explanation and report link. |

## 11. Remaining Boundaries

The guarantee is **at-least-once delivery + idempotent/recoverable execution +
owner-safe renewable mutual exclusion within a valid Redis lease**. Confirmed
loss or locally expired ownership evidence stops cooperative work and blocks
new guarded submissions; the old owner does not write a terminal lifecycle or
clear a successor's active reservation as a normal failure.

An already-dispatched to_thread transaction or third-party HTTP/tool operation
cannot be rolled back by asyncio cancellation. Its response/side effect may
settle later. Guard checks and database commit are not an atomic Redis/SQL
fenced transaction. A long process pause, Redis eviction/failover, or external
side effect can still require recovery/idempotency and may permit overlap of
already-issued I/O. Forced token replacement before the next check is not
continuously observed. Permanently noncooperative code can delay task joining;
the loop is not killed to hide that limitation. This change does not add fencing
tokens, durable broker publisher confirms or an exactly-once transaction.

Lease expiry preserves existing attempt semantics: a successor starts/resumes
through the bounded lifecycle, with durable checkpoint/result/handoff authority.
Repeated process crashes can consume attempts; transport retries are unlimited
while business attempts are not. Locks do not replace this recovery state.

## 12. Interview Explanation

### 30 秒版本

VideoMind 的异步分析采用 at-least-once 消息处理，消费者不能假设只收到一次。
Redis TaskLock 控制同一任务的并发。长视频可能超过固定 15 分钟租期，所以我借鉴
WatchDog 思想，在 Python Worker 内实现自动续租：每租期三分之一检查 owner token
并原子延长 TTL；任务结束先停止续租再安全释放。Worker 崩溃后续租停止，锁自动
到期，消息和 checkpoint 可恢复。旧 token 不能误续租或误释放新 owner 的锁。

### 2 分钟版本

HTTP 先保存任务和 active 预约，再把请求交给 RabbitMQ/Celery。消息可能重复，
消费者在 AgentLoop 前拿按 media、goal、mode 区分的 Redis 锁。原来有随机 token、
SET NX PX 和 owner-safe release/refresh，但 Worker 没有定期调用 refresh；长任务
超过 15 分钟后，新 Worker 能同时进入 Agent。仅增大 TTL 会延迟崩溃恢复，也不能
覆盖任意长任务。

我把租期和 refresh 暴露在应用接口，Worker 获锁后启动一个小的 LeaseKeeper，
按 TTL/3 续租，仍使用既有 Lua 的 GET==token 再 PEXPIRE。短暂 Redis 异常只在已知
有效期内有限等待和重试；返回 false 或已知租期耗尽时，取消可协作工作，在 Worker、
checkpoint 和执行记录存储入口阻止后续提交，保留恢复预约，让 adapter 重新投递。
正常完成仍遵守 result、lifecycle、completion marker 的顺序。所有退出路径都会
停止并回收续租，再 owner-safe release。LOCKED 消息单独延迟 5 秒重试，不消耗分析
attempt，避免竞争空转。

测试用短 TTL 验证长任务续租、重复排斥、旧 owner 不能修改新锁、丢租不能最终提交，
并真实强制结束 RabbitMQ worker，验证 TTL 到期、消息重投和 checkpoint 恢复。
这解决有效租期内互斥与故障恢复，但已经发出的线程或供应商请求未必可撤销，也没有
数据库 fencing。因此我只表述 at-least-once 加幂等恢复，不声称 exactly-once。

### 追问

| 问题 | 回答 |
| --- | --- |
| 为什么不用 Redisson？ | 它是 Java 客户端；Python 已有安全 Lua，只需要补应用续租生命周期。 |
| 为什么不能只调大 TTL？ | 任意长任务仍会越界，且崩溃后等待更久；有限租期加续租兼顾两者。 |
| WatchDog 怎么工作？ | 获锁后定期验证 token 并延长 TTL；结束回收，进程死后没有心跳。 |
| Worker crash 怎么办？ | 续租停止，Redis TTL 到期；late ACK/reject-on-worker-lost 配合重投，恢复已有 checkpoint。 |
| refresh 为什么校验 token？ | 防止 A 的迟到请求续上 B 的锁；比较和 PEXPIRE 必须原子。release 同理。 |
| ACK 丢失会怎样？ | 重复投递；执行中的锁排斥重复，已存结果走恢复路径。 |
| 为什么还需要幂等/Checkpoint？ | 锁只约束同时执行，不能消除时间上重复、部分提交和外部副作用。 |
| 锁能否保证 exactly-once？ | 不能；消息、Redis、数据库和外部服务没有一个原子事务，租期与 I/O 也有边界。 |

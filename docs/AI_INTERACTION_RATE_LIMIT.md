# AI interaction request-level Token Bucket

## Current-source audit

Before this change, main `9dbdb0c97e1d26c45fc90b048caa064a425c5037`
implemented **Redis + Lua dual-dimension fixed-window limiting**, not Token
Bucket. Redis used String counters with GET/INCR/EXPIRE and per-key first-use
windows; Local/Test reset all user counters with the global window. The shared
application Port, production Redis composition, local adapter, SHA-256 user
keys, all-or-nothing deduction, disable switch, bounded observations, safe
429/Retry-After and fail-closed 503 were already present and are retained.

Gaps were continuous refill, token state, shortage-based Retry-After, a safe
String-to-Hash migration, and admission placement. Analysis, follow-up and
evidence search previously admitted before ownership. Analysis also admitted
before completed-result reuse. Route already validated before admission.

## Policy and algorithm

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `DOVIDEO_AI_RATE_LIMIT_ENABLED` | `true` | Explicit admission enable switch |
| `DOVIDEO_AI_USER_RATE_LIMIT` | `60` | User bucket capacity |
| `DOVIDEO_AI_GLOBAL_RATE_LIMIT` | `600` | Shared global bucket capacity |
| `DOVIDEO_AI_RATE_WINDOW_SECONDS` | `60` | Duration to refill an empty bucket to full |

Refill rate is capacity / full refill duration: defaults are 1 token/s for
each user and 10 tokens/s globally. All four endpoints cost exactly 1 token.
New buckets start full, then consume the current request. This permits a
bounded burst and controls the long-term average admission rate. For fixed
configuration, admissions in an interval are bounded by initial available
tokens plus tokens refilled during that interval; there is no window reset.

The Redis adapter executes a single EVAL containing:

1. Redis TIME converted to milliseconds, shared by both buckets.
2. HMGET `tokens` and `last_refill_ms` for each bucket.
3. `min(capacity, tokens + elapsed_ms * capacity / window_ms)` for both.
4. Check both refilled balances against cost 1.
5. Deduct one from both only if both suffice; otherwise deduct neither.
6. Persist refilled balances/timestamps and renew safe idle TTLs on either
   outcome, then return allowed, reason and retry wait.

Lua execution serializes admission across API instances using shared Redis
state. Python process locks are not the distributed correctness mechanism.
Both bucket reads/refills happen before writes, so a state read/type error
does not first spend one dimension. Redis Lua atomicity is isolation, not a
general transaction rollback guarantee for arbitrary Redis server failures.

Tokens are floating point and serialized with 17 significant digits. Strict
`>= 1` comparisons introduce no epsilon that could grant an extra request.
Refill is capped at capacity. A backward Redis clock step freezes the saved
timestamp rather than granting elapsed time twice; shortage waits include
that clock gap. The local adapter mirrors the algorithm with an injectable
monotonic clock and a local lock; inactive user state is periodically pruned
only after it would be full. It is development/test state, not distributed.

## Keys, TTL and migration

Production keys are:

```text
dovideo:ai-interaction:v2:user:<SHA-256(decimal authenticated user ID)>
dovideo:ai-interaction:v2:global
```

Both are Hashes with `tokens` and `last_refill_ms`. TTL is twice the configured
full refill duration: 120 seconds by default. Even a completely empty bucket
would be full after half that idle TTL, so natural expiry and recreation at
full capacity preserve semantics. Every admitted or rejected interaction
renews TTL; active denials cannot induce early reset. The global key uses the
same safe TTL because the full refill duration is identical.

Old unversioned String keys remain untouched and expire under their existing
TTL. The new namespace prevents WRONGTYPE even when old counters exist. No
Redis flush, key deletion or fixed-window compatibility mode is needed.
Migration deliberately starts fresh buckets, allowing one initial capacity
burst. Old and new deployments use independent quotas: coordinate the API
rollout rather than claim a shared aggregate limit during mixed-version
operation. Live changes to capacity/refill settings should also be coordinated
across API instances; state is interpreted under the caller's policy.

The project's current standalone Redis composition is supported. This change
does not introduce Redis Cluster hash-slot placement or claim failover-proof,
exactly-once accounting across lost Redis writes.

## API admission and failure behavior

| Surface | Order after authentication |
| --- | --- |
| `/analysis/route` | Validate goal -> Token Bucket -> Router/model |
| `/analysis/ai` | Validate goal/mode/ID -> ownership -> completed-result lookup/reuse -> Token Bucket -> submit_analysis -> dispatcher -> RabbitMQ -> Celery |
| `/analysis/follow-up` | Validate question/goal/mode/ID -> ownership -> Token Bucket -> retrieval/LLM |
| `/analysis/evidence-search` | Validate query/ID -> ownership -> Token Bucket -> retrieval |

Ownership precedes completed-result lookup to preserve the cross-user security
boundary. Existing completed-result responses, task keys, active markers,
dispatcher duplicate handling (409), task locks and checkpoints are retained.
Active duplicates may still spend request admission tokens; admission is
separate from task idempotency and is not refunded for downstream failure.
Status, task event/SSE, ordinary media and historical reads remain unmetered.

Invalid input returns 400 and unauthenticated requests return 401 before
admission. Foreign/missing media return existing 403/404 without admission.
Completed-result reuse returns the existing 200 contract without admission.
Insufficient tokens return safe 429 with `Retry-After`:

```text
max(1, ceil(max(user_missing / user_refill_rate,
                global_missing / global_refill_rate)))
```

The reason remains `USER_LIMIT` when the user is short (including both-short),
otherwise `GLOBAL_LIMIT`. The wait always considers both buckets. It is the
earliest theoretical wait under no competing consumption, not a reservation.
Redis unavailability or an invalid backend decision raises the typed limiter
unavailable error, maps to safe 503, and prevents expensive dispatch/retrieval.
Redis details never enter public error messages.

Observations remain bounded endpoint + outcome: `ALLOWED`, `USER_LIMIT`,
`GLOBAL_LIMIT`, `BACKEND_FAILURE`, `ALLOWED_DISABLED`. No user ID, goal,
question or query is a log/metric label. Raw user IDs never appear in keys.

## Responsibility boundaries and non-goals

This is **request-level distributed rate limiting**. It is not LLM TPM
enforcement, exact monetary cost control, GPU quota, weighted endpoint costs,
video-duration weighting, a sliding window or a leaky bucket. No Redisson,
Java dependency, limiter framework, gateway or Nginx limiter is introduced.

Token Bucket decides whether work may enter. RabbitMQ/Celery queue, consume
and recover work already accepted; task idempotency prevents duplicate
execution. The dispatcher quota, broker topology, Celery retry policy and
worker locking are unchanged. Admission stays before MQ, not in a worker.
Upstream is design context only, never the Python implementation authority.

## Validation and reproduction

```powershell
.venv/Scripts/python.exe -m pytest -q tests/infrastructure/test_ai_interaction_limiter_p5.py tests/presentation/test_api_ai_interaction_rate_limit_p5.py
.venv/Scripts/python.exe scripts/run_ai_limiter_live_tests.py --env-file /path/to/ignored/infrastructure.env
.venv/Scripts/python.exe -m pytest -q
```

The live runner loads only `DOVIDEO_REDIS_URL`, never prints credentials, and
enables real EVAL tests. Tests use a fresh UUID namespace and remove only
their keys. An explicitly enabled run fails if Redis is unavailable; offline
pytest skips that suite unless explicitly enabled. Real Redis coverage includes
burst, partial/full refill, capacity cap, no fixed-window reset, both-or-none
deduction, concurrent clients, safe expiry, shortage waits, hashed keys and old
String coexistence. Local/API coverage includes deterministic boundary times,
concurrency, disabled mode, fail-closed behavior, ownership/reuse ordering,
exactly one admission per valid request and unmetered status.

## Interview answers

Validation snapshot (2026-10-07): unit/API suite **52 passed**; actual Docker
Redis 7.4 suite **14 passed**; presentation + Celery/dispatcher regression
**105 passed**; full backend **1128 passed, 31 skipped**; frontend **79 passed**
and production build passed. The full offline run skips 14 Redis limiter tests
which were separately executed live, plus 17 pre-existing opt-in infrastructure
tests. Compileall, deployment environment/composition validation, Compose
configuration, Alembic repeated upgrade/head/drift and diff checks passed.
An existing Starlette/httpx deprecation warning remains. Initial Windows
sandbox/socket and pre-existing pytest temp-directory permission issues were
resolved by authorized host execution and fresh isolated test directories.

**30 seconds:** VideoMind 在四个 AI 交互入口使用 Redis + Lua 实现用户级和
全局级双维分布式令牌桶。一个脚本基于 Redis TIME 连续补充两个桶，只在
两桶都有额度时各扣一个请求 token，允许有限突发并约束长期平均速率。
分析先校验权限、复用完成结果，再限流，最后入 RabbitMQ；Redis 故障时
fail-closed 返回 503。

**90 seconds:** 原实现是 INCR + EXPIRE 固定窗口，我保留了已有 Port、Redis
wiring、429、Retry-After 和观测接口，把状态升级为 tokens 与 last_refill_ms。
默认用户容量 60、全局容量 600，60 秒补满，即每秒补 1 与 10 个请求 token。
Lua 用 Redis TIME 统一多实例时钟，原子 refill、检查和双桶扣减；任何一桶
不足都不扣业务 token，Retry-After 取两桶缺额等待时间的最大值并向上取整。
Hash 使用 v2 namespace 避免旧 String key 的 WRONGTYPE；120 秒闲置 TTL
保证过期时桶早已补满，活跃请求不会因过期绕过限速。入口先做认证、参数
校验和 ownership，analysis 再检查完成结果复用，之后才限流、dispatch 与
MQ 入队。令牌桶负责准入，RabbitMQ/Celery 负责已接受任务的排队恢复，任务
幂等负责避免重复执行。Redis 异常返回安全 503，额度不足返回 429；真实
Redis 测试验证了 Lua、并发原子性和迁移。这是请求速率控制，LLM token
预算由独立模块管理。

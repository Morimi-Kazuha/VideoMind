# Analysis lease source audit — 2026-10-06

Before implementation: clean isolated `main`, HEAD
`803b93b3c297642814837d16ada6a860ba2143e6`, equal to remote main.
The original `dovideo-python` checkout contains unrelated work and is untouched.

## CURRENT

HTTP production R4 services inherit R3 dispatch: ownership validation, active
reservation (6 hours), QUEUED lifecycle and request ID, then JSON broker enqueue.
RabbitMQ durable exchange/queue routes to the registered Celery analysis task.
Its default runtime is **R4WorkerRuntime**, not the R3 smoke runtime. Both R4
worker and R3 API composition inject `R2Infrastructure.task_lock` into TaskWorker;
R2 constructs RedisTaskLock. R3 smoke alone permits a short test lease override.

TaskLockPort exposes acquire/release only. RedisTaskLock already has refresh.
Its key is `lock:analysis:{media_id}:{goal_digest(goal, mode)}`; tokens are
`secrets.token_urlsafe(32)`. Acquire is SET NX PX, default TTL 900,000 ms.
Release atomically compares GET/token before DEL; refresh atomically compares
GET/token before PEXPIRE. Neither can modify another owner's lease.

TaskWorker acquires before loading lifecycle, returns LOCKED on contention,
and releases in finally. There is no renewal caller. It checks stale request
IDs, pending durable DLQ handoffs, saved results and terminal lifecycle before
starting an attempt. AgentLoop cooperatively propagates cancellation and joins
provider child tasks. **AgentLoop itself saves result checkpoints**, including
the terminal-checkpoint shortcut; guarding only Worker.save_result is inadequate.
Checkpoint service and execution-record service have central async repository
bridges using to_thread: these are suitable additional lease safety boundaries.
Worker success ordering is result -> COMPLETED lifecycle -> completion marker.
Transient business failures retain active markers and increment bounded attempts;
terminal failure persists pending handoff before lifecycle/DLQ publication.
Result recovery and durable handoff recovery do not rerun AgentLoop.

Celery uses late ACK, reject-on-worker-lost, failure/timeout ACK disabled,
prefetch multiplier 1. COMPLETED/STALE/DEAD_LETTERED return for ACK. RETRY and
LOCKED both call retry(countdown=0.25, max_retries=None). Unexpected recovery
exceptions also use transport retry; TaskWorker lifecycle remains the business
attempt counter. LOCKED does not increment attempts but retries too frequently.
Process death stops execution; Redis TTL eventually expires and unacknowledged
broker delivery is recoverable. A saved result repairs lifecycle without rerun.

Upload MergeLock uses redis-py Lock, a scoped lease with ttl_seconds/refresh,
and a bounded merge deadline. It remains independent and will not be changed.

## TARGET / GAP

A acquires at t=0 and enters AgentLoop. At t=900s Redis expires its fixed lease
while A remains alive. Duplicate B obtains a new token and enters AgentLoop:
two live executions can write checkpoints/results for the same key.

Expose provider-neutral lease_seconds/refresh; add a small per-delivery keeper
with interval TTL/3, conservative local expiry and bounded refresh waits. On
confirmed loss or inability to renew before the known expiry, cancel cooperative
work and block subsequent worker/checkpoint/history writes. Preserve active
reservation and route the delivery through transport recovery, without stale
failure/completion writes. Join renewal before releasing the owner-safe lock.
Give LOCKED a separately configured positive delay; preserve business retries.

## Limits to validate

This is at-least-once plus recoverable/idempotent execution and lease-based
mutual exclusion, not exactly-once. Redis ownership checks and database writes
are not one fenced transaction. Already-dispatched thread/third-party I/O may
finish after cancellation; event-loop suspension and Redis failover remain
distributed-lock limitations. Tests must distinguish these from preventing
**new** writes after known loss or locally expired ownership evidence.

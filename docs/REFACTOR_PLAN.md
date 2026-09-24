# DOVideo Python — Canonical Roadmap

R0, R1, and R2 are accepted/frozen. R3 is implemented and live-verified as
the production Celery/RabbitMQ transport boundary over the existing R2
infrastructure and frozen TaskWorker semantics. The current handoff is
waiting for Web Sol sign-off. No later phase is active.

## R0 — Scope Realignment & Architecture Consolidation (accepted/frozen)

Audit the public Java repository and the Python workspace, lock Category A
parity and Category B enhancements, classify fallbacks and the legacy demo,
restore Vue as the final frontend direction, classify the original
timestamp-evidence/evaluation/trace baselines, and bound the interview scope.

R0 output: docs/SCOPE_LOCK.md, docs/PARITY_MATRIX.md,
docs/INTERVIEW_SCOPE.md, the consolidated architecture/readme/handoff, and
LUNA_REPORT.md. Runtime semantics remain frozen.

## R1 — FastAPI + Vue + SSE Product Parity (accepted/frozen)

Build and preserve the original product-facing HTTP path with FastAPI,
Pydantic request and response contracts, Vue 3 + Vite, upload/task/result
behavior, original follow-up surfaces, the `/analysis/agent-evaluation` and
`/analysis/agent-trace` baseline contracts, and SSE progress/result delivery.
The R1 local transport remains available only for explicit local/test use.

## R2 — SQLAlchemy/MySQL/Redis/MinIO/Qdrant (accepted/frozen)

COMPLETED — the canonical production data and storage path is:

- SQLAlchemy 2.x and MySQL for durable truth;
- Redis for hot state, locks, idempotency, quotas, and telemetry persistence;
- MinIO for media and resumable object storage;
- Qdrant through the existing vector adapter.

The existing SQLite, memory, DB-API compatibility, and local embedding
adapters remain explicit local/test/internal roles. R2's four infrastructure
services remain live and are not replaced by R3.

## R3 — Celery + RabbitMQ Async Parity (completed; awaiting sign-off)

COMPLETED — add only the production asynchronous transport:

```text
FastAPI → TaskDispatchService → CeleryTaskTransport → RabbitMQ
        → existing TaskWorker → existing AgentLoopService → R2 services
        → existing TaskEventDeliveryService / status / SSE
```

The implementation is in `src/dovideo/infrastructure/celery_transport.py`,
`celery_tasks.py`, `celery_runtime.py`, and `celery_worker.py`, with
`ProductionR3Services` selected only by `DOVIDEO_PROFILE=production`.

R3 guarantees only the tested semantic boundary: bounded JSON messages,
late-ack at-least-once transport, existing TaskWorker business attempts 1/2/3,
durable result/lifecycle recovery, explicit business failure versus RabbitMQ
DLQ state, and a recoverable dead-letter handoff. It does not claim exactly-once
execution and does not move SSE onto RabbitMQ.

RabbitMQ is pinned to `rabbitmq:3.13.7-management`, with D-backed persistent
data and explicit main exchange/queue plus DLX/DLQ. The real live smoke covers
success, transient retry, permanent failure, poison convergence, result-saved
recovery, worker interruption/redelivery, active-marker retention, and the
post-restart healthy broker.

## R4 — Full Original-Parity E2E (future; not started)

Prove the complete provider-backed path:

Vue → FastAPI → upload → MinIO → Celery/RabbitMQ → worker → ASR/OCR →
VideoContext → Qdrant hybrid retrieval → AgentLoop → MySQL/Redis checkpoint →
SSE → result/evidence in Vue.

R4 owns the representative long-video full product proof, real provider
composition, and final original-parity E2E acceptance. It must begin only
after R3 Web Sol sign-off.

## X1 — Restricted Tool Calling (complete)

X1-D2 closes the restricted model-issued tool path. Production uses an
explicit opt-in feature flag, a static registry containing only
`video.search_evidence`, `video.get_segment`, and `video.get_context_window`,
deterministic policy, same-video trusted identity, durable-before-execution
state, result-level recovery deduplication, bounded Redis trace counters, and
the existing Evidence Guard. The feature remains disabled by default.

## X2 — Evidence Provenance + Trace/Replay (future; not started)

Extend the original timestamp/source evidence and AgentTelemetry baseline with
the approved answer-provenance chain and a structured trace_id execution chain
from Planner through retrieval/tools, Executor, Critic, Evidence Guard, and
final result. Support replay/inspection of one recorded execution without
introducing Event Sourcing or a second trace system.

## X3 — Evaluation/Ablation (future; not started)

Extend the original AgentEvaluationService metric path with Retrieval Recall@K,
Evidence Hit Rate, Groundedness, Critic Pass Rate, Agent Round, Token, Cost,
Latency, and bounded ablation experiments over real outputs/traces.

## Execution stop

X1-D2 is complete and the workspace is stopped at the Web Sol review
boundary. Do not start X2 or X3 from this handoff.

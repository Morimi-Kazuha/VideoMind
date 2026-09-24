# DOVideo Python — Scope Lock

Status: R0 — Scope Realignment & Architecture Consolidation

This document is the canonical boundary for the Python reconstruction. It
keeps the original DOVideo-AI product behavior as the primary study and
implementation target, while allowing only four explicitly approved Python
enhancement families. Where the original already has timestamp evidence,
Agent Evaluation, and AgentTelemetry/Trace behavior, those are Category A
parity baselines; the approved families are deltas on top of those baselines.

## Project purpose

DOVideo Python is a Python-native reconstruction of the public
DOVideo-AI Java/Spring Boot project. It is not a line-by-line translation, a
generic Agent framework, a microservice rewrite, or a technology showcase.

The reconstruction must preserve the original product concepts:

- reliable video upload and asynchronous analysis;
- temporal multimodal VideoContext built from ASR, OCR, frames, and timestamps;
- five-minute long-video chunking and hybrid evidence retrieval;
- an explicit Planner–Executor–Critic AgentLoop with evidence verification;
- durable checkpoint/resume and task lifecycle behavior;
- product-facing progress and result delivery.

## Category A — Original project parity

The final production direction preserves these original capabilities.

### Product and infrastructure

- Vue 3 + Vite product frontend with SSE;
- FastAPI API with Pydantic contracts;
- SQLAlchemy 2.x over MySQL;
- Celery + RabbitMQ as the Python async transport;
- Redis for hot state, locks, idempotency, and rate guards;
- MinIO for video and evidence-media persistence;
- Qdrant for the production vector index;
- FFmpeg, ASR, and OCR media processing;
- BGE-M3 or the existing approved semantic embedding path;
- a structured LLM provider.

### Agent and retrieval behavior

- explicit Python Agent Runtime and AgentState;
- Planner, plan repair, Executor, Executor checkpoint, and Critic;
- Evidence Verification as an authoritative application gate;
- original timestamp/source/claim evidence grounding;
- original Agent Evaluation endpoint and metric baseline;
- original Agent Trace endpoint and AgentTelemetry baseline, including its
  Redis persistence/latest-lookup behavior;
- Critic-driven targeted retrieval retry;
- context and Agent budgets;
- Analysis Mode and ModeProfile;
- checkpoint and resume;
- Retrieval Intent / query planning, hybrid retrieval, and temporal evidence.

### Product/backend behavior

- video upload and original resumable/chunked-upload semantics;
- asynchronous submission and task IDs;
- duplicate and idempotency protection;
- task stages, retry, failure, and dead-letter handling;
- durable MySQL truth, Redis hot state, MinIO media, and Qdrant retrieval;
- SSE progress and final-result delivery;
- original follow-up behavior where present in the public product.

## Category B — Approved Python enhancements (deltas only)

Only these additional capability families are in scope. They do not replace or
reclassify the original timestamp-evidence, evaluation, or AgentTelemetry
baselines as Python inventions:

1. Deep Evidence Provenance
2. Restricted Tool Calling
3. Structured Execution Trace / Replay
4. Advanced Evaluation / Ablation

Their boundaries are intentionally narrow.

### Deep Evidence Provenance

The original parity baseline already binds evidence to text/source and a
covered timestamp. The approved delta adds the deeper lineage:

result conclusion → evidence → ASR/OCR source → VideoContext → timestamp or
context window → original video.

This strengthens the original timestamp-evidence design. It is not a separate
knowledge graph or a general provenance platform.

### Restricted Tool Calling

Only these tools are approved:

- search_video
- get_segment
- get_transcript
- find_visual_evidence

The implementation must use a registry, explicit schemas, parameter
validation, allow-list permissions, bounded execution, normalized results, and
trace integration. It must never execute arbitrary Python, shell, or network
operations selected by a model.

### Structured Execution Trace / Replay

The original AgentTelemetry/agent-trace behavior is the baseline. One Agent
run may extend that same trace_id with structured records for Planner,
retrieval intent/query, retrieved chunks, approved tools, Executor, Critic,
evidence verification, rounds, token usage, cost, and latency. Replay means
inspecting or replaying one recorded Agent execution chain. It does not mean
Event Sourcing or a second trace subsystem.

### Advanced Evaluation / Ablation

The original AgentEvaluationService metric map is the baseline. The approved
delta adds Retrieval Recall@K, Evidence Hit Rate, Groundedness, Critic Pass
Rate, Agent Round, Token, Cost, Latency, and bounded ablation experiments over
real outputs/traces where possible. This is not ML training infrastructure or
a second evaluation subsystem.

## Forbidden scope expansion

The following are explicitly outside the project unless Web Sol approves a
new scope:

- React, Next.js, or another frontend framework;
- microservices, Kubernetes, service mesh, or speculative cloud deployment;
- Kafka or any second production message queue;
- another production database, vector database, or object store;
- Event Sourcing;
- an arbitrary Agent framework or unrestricted ReAct;
- MCP added only for trend-following;
- authentication redesign beyond original parity;
- generic observability platforms or unrelated dashboards.

No R1 work is implied by this document. R0 does not implement the future
FastAPI/Vue, SQLAlchemy/MySQL/Redis/MinIO, Celery/RabbitMQ, original
evaluation/trace product surfaces, or the tool-calling, deep-provenance,
structured-replay, and advanced-evaluation stages.

## Original → Python technology mapping

| Original Java implementation | Python reconstruction |
| --- | --- |
| Vue 3 + Vite | Vue 3 + Vite |
| Spring Boot | FastAPI |
| Java DTO and validation | Pydantic |
| MyBatis-Plus | SQLAlchemy 2.x |
| RocketMQ | Celery + RabbitMQ |
| Redis / Redisson | Redis Python adapters |
| MySQL | MySQL through SQLAlchemy 2.x |
| MinIO | MinIO Python adapter |
| Qdrant | Qdrant Python adapter |
| FFmpeg | FFmpeg subprocess adapter |
| ASR / OCR | Python ASR and OCR adapters |
| LangChain4j / Java Agent glue | explicit Python Agent Runtime |
| Spring SSE | FastAPI SSE |

The mapping is deliberately one-to-one at the concept level. Provider or
transport adapters must not create a second application algorithm.

## Current role classification

The accepted Python core is retained. Current concrete roles are:

| Current component | Canonical role now |
| --- | --- |
| SQLite checkpoint and media stores | test/local fallback |
| in-memory cache and upload/object stores | test/local fallback |
| in-memory vector index | test/local fallback for the developer composition |
| Local TF-IDF | offline/test/fallback embedding |
| OpenAI-compatible BGE-M3 adapter | production semantic embedding path |
| Qdrant adapter | production vector path, not yet wired into the local composition |
| DB-API MySQL and Redis adapters | production-boundary adapters, live wiring deferred to R2 |
| TaskLifecycle, TaskWorker, checkpoint recovery, and dead-letter handoff | reusable business semantics/internal reliability implementation |
| CLI | developer/debug utility, not the main product path |
| standard-library Web Demo | legacy temporary presentation, not final product architecture |

Fallbacks are not deleted merely because production adapters are planned.
They must remain visibly labeled and must not be presented as equal production
paths.

## One canonical production path per responsibility

The final path is:

Vue 3 + Vite → FastAPI → application services → Celery + RabbitMQ →
existing task/lifecycle semantics → media and Agent pipeline → MySQL,
Redis, MinIO, and Qdrant → FastAPI SSE → Vue.

There is one canonical production implementation per responsibility:

- CheckpointRepository: MySQL production adapter; SQLite is test/local.
- CachePort: Redis production adapter; memory is test/local.
- EmbeddingPort: BGE-M3 semantic adapter; TF-IDF is fallback/test.
- VectorIndexPort: Qdrant production adapter; memory is test/local.
- Task execution: existing TaskWorker/application semantics; Celery/RabbitMQ
  is the production transport adapter.
- Frontend: Vue 3 + Vite; the old Web Demo is temporary and React is not a
  supported path.
- Agent trace: one original AgentTelemetry-compatible service, extended by X2;
  no duplicate trace/telemetry subsystem.
- Agent evaluation: one original AgentEvaluation-compatible service, extended
  by X3; no duplicate metrics subsystem.

## Locked roadmap

The only remaining roadmap stages are:

1. R0 — Scope Realignment & Architecture Consolidation
2. R1 — FastAPI + Vue + SSE Product Parity
3. R2 — SQLAlchemy/MySQL/Redis/MinIO/Qdrant
4. R3 — Celery + RabbitMQ Async Parity
5. R4 — Full Original-Parity E2E
6. X1 — Restricted Tool Calling
7. X2 — Evidence Provenance + Trace/Replay
8. X3 — Evaluation/Ablation

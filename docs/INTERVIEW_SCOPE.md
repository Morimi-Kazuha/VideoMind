# DOVideo Python — Interview Scope

This document limits the study burden. The original author's documentation is
the primary template; only the Python mappings and the four approved
enhancement families need to be added.

## Original project concepts to learn

Learn the original behavior first, then the direct Python replacement:

| Original concept | Python concept to learn |
| --- | --- |
| Spring Boot API | FastAPI |
| Java DTO and Bean Validation | Pydantic |
| MyBatis-Plus | SQLAlchemy 2.x |
| RocketMQ | Celery + RabbitMQ |
| MySQL durable truth | SQLAlchemy/MySQL repository |
| Redis/Redisson cache, locks, quotas | Redis Python adapters |
| MinIO object storage | MinIO adapter |
| Qdrant vector retrieval | Qdrant adapter |
| FFmpeg, ASR, and OCR | Python media adapters |
| VideoContext | timestamped multimodal Python domain model |
| five-minute chunks and hybrid retrieval | VideoChunkingService and VideoEvidenceRetrievalService |
| AgentLoop | explicit Python Agent Runtime |
| AgentState, Planner, plan repair, Executor, Critic | existing Python AgentLoop contracts |
| Evidence Verification | authoritative Evidence Guard |
| Timestamp evidence grounding | EvidenceVerificationService and half-open VideoContext timestamp coverage |
| Agent Evaluation endpoint and baseline metrics | AgentEvaluationService and its structured/evidence/critic/feedback metric map |
| Agent Trace endpoint and AgentTelemetry baseline | AgentTelemetry trace identity, stages, counters, token/cost estimates, and numeric values |
| Trace Redis persistence/latest lookup | Redis snapshot/index keys with bounded retention and task/goal lookup |
| Checkpoint and resume | checkpoint service plus task-worker recovery |
| task stages and status | TaskLifecycle and status projection |
| Spring SSE | FastAPI SSE |
| Vue 3 + Vite workbench | Vue 3 + Vite product client |
| original follow-up/evidence/feedback behavior | FastAPI product endpoints over the same media/task identity |

These are Category A parity concepts. The evaluation and trace surfaces are
original behavior to reproduce, not Python-only inventions. Do not replace
them in interview preparation with generic Agent or platform terminology.

## Approved enhancement concepts to learn (deltas after the original baseline)

Only these four additions are part of the Python-specific interview boundary;
they extend the original evidence, evaluation, and telemetry behavior where
those baselines already exist:

1. Deep Evidence Provenance: result → evidence → ASR/OCR → VideoContext →
   timestamp/window → original video.
2. Restricted Tool Calling: four allow-listed video tools with schemas,
   permission checks, validation, bounds, normalization, and trace links.
3. Structured Execution Trace / Replay: one extension of the original
   AgentTelemetry trace_id containing the Planner → Retrieval/Tools → Executor
   → Critic → Evidence Guard → Result chain.
4. Advanced Evaluation / Ablation: extend the original AgentEvaluationService
   metrics with Recall@K, Evidence Hit Rate, Groundedness, Critic Pass Rate,
   Agent Round, Token, Cost, Latency, and bounded ablations.

Nothing in this section authorizes Event Sourcing, unrestricted ReAct, an
arbitrary Agent framework, or a new observability platform.

## Internal implementation details

These are useful when reading the code but do not require primary interview
preparation:

- status projection internals;
- provider HTTP/configuration plumbing;
- provider-neutral ports and adapter aliases;
- task-key normalization and retry-cause inspection;
- durable dead-letter handoff ordering;
- SQLite checkpoint/media implementation;
- in-memory cache, upload/object, and vector stores;
- local TF-IDF vocabulary construction;
- local Chunk Summary fallback;
- CLI composition and debugging output;
- legacy standard-library Web Demo implementation.

Describe these as implementation details or test/local adapters. Do not
present them as extra product capabilities or competing production paths.

## Bounded study order

1. Original reliable task chain and media upload.
2. Temporal VideoContext and long-video retrieval.
3. Evidence-constrained AgentLoop and checkpoint/resume.
4. Direct Java → Python technology mapping.
5. The four approved enhancements.

The canonical frontend is Vue 3 + Vite. React is not part of the study
boundary.

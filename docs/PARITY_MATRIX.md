# DOVideo Python — Original Parity Matrix

This matrix compares the current public original repository on its main branch
with the current Python workspace. The original audit used the repository
README, client tree/package manifest, server controllers/services, AI
configuration, and Docker Compose definition. The Python audit covered the
source tree, application ports/services, infrastructure adapters, composition
root, tests, README, and handoff/report documents.

R2 SQLAlchemy/MySQL/Redis/MinIO/Qdrant composition and R3
Celery/RabbitMQ transport are implemented and live verified on Docker Desktop
with the Docker/WSL data root on D:. MySQL, Redis, MinIO, Qdrant, and RabbitMQ
are healthy. The bounded R3 smoke covers durable lifecycle/result recovery,
late-ack redelivery, worker restart, bounded business attempts, explicit
business failure versus DLQ handoff, and unchanged status/SSE projection. See
`LUNA_REPORT.md` and `HANDOFF.md` for the complete evidence and final boundary.

Original references:

- https://github.com/Xiaoc7r/DOVideo-AI
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/README.md
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/resources/application.properties
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/controller/AnalysisController.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/AnalysisDispatchService.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/ChunkUploadService.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/VideoChunkingService.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/AgentCheckpointService.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/AgentLoopService.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/AgentEvaluationService.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/AgentTelemetry.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/OfflineAgentEvaluationRunner.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/service/AiService.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/utils/EmbeddingUtils.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/server/src/main/java/com/example/server/utils/DeepSeekUtils.java
- https://raw.githubusercontent.com/Xiaoc7r/DOVideo-AI/main/docker-compose.yml

## Status vocabulary

- PARITY_COMPLETE: the Python contract/behavior is implemented and covered at
  the current local boundary; this does not claim that future deployment
  wiring is already live.
- PARITY_PARTIAL: meaningful behavior or an adapter exists, but the original
  product/infrastructure path is incomplete or not yet composed.
- PARITY_MISSING: the required capability is not present in the current
  Python product path.
- APPROVED_ENHANCEMENT: one of the four permitted Python enhancement families;
  its implementation state is stated separately.
- INTERNAL_IMPLEMENTATION_DETAIL: retained code that is not a headline
  product capability.
- TEST_FALLBACK: local/test implementation, not the production role.
- TEMPORARY_DEMO: useful legacy presentation code, not the final product path.
- OUT_OF_SCOPE: an explicitly rejected or absent alternative.

## Original capability matrix

| Original Capability | Original Implementation | Python Current Implementation | Status | Final Canonical Implementation | Action / Remaining Stage |
| --- | --- | --- | --- | --- | --- |
| Video upload | MediaController, MediaService, and Vue upload workflow | FastAPI `/media/upload` over `ProductionR2Services`; MinIO object persistence; original Vue workflow | PARITY_COMPLETE | FastAPI upload API backed by MinIO | R2 complete |
| Chunk/resumable upload | Vue 5 MiB chunks; Redis metadata; MinIO merge; ownership and expiry | application media contracts and ChunkUploadService composed with Redis session/lock state and MinIO chunk objects; live duplicate/ownership/completion verification | PARITY_COMPLETE | FastAPI resumable upload over Redis + MinIO | R2 complete |
| MinIO media persistence | MinioUtils and merged object storage | `MinioObjectStorage` and `MinioChunkObjectStore` in the production R2 composition; live object recovery verified | PARITY_COMPLETE | MinIO | R2 complete |
| Async submission | AnalysisController returns 202 and dispatches analysis task | FastAPI `/analysis/ai` → existing TaskDispatchService → Celery/RabbitMQ → existing TaskWorker | PARITY_COMPLETE | FastAPI submission to Celery | R3 complete; local transport remains explicit local/test only |
| MQ transport | RocketMQ template, topic, consumer, and broker Compose service | Celery JSON task, RabbitMQ durable exchange/queue, explicit DLX/DLQ, and real worker entrypoint | PARITY_COMPLETE | Celery + RabbitMQ | R3 complete; semantic mapping documented |
| Duplicate protection | content/goal task key, Redis active marker, Redisson lock, completed result | AnalysisTaskKeys, lifecycle and active-marker contracts, Redis active/completion markers and token-safe locks in the R2 composition | PARITY_COMPLETE | Redis idempotency and locking | R2 complete; transport remains R3 |
| Rate/cost guard | user/global Redisson token buckets plus Agent token/cost budgets | Agent budget and usage validation plus the Redis quota adapter/composition; no transport expansion | PARITY_COMPLETE | Redis rate guard plus Agent budget | R2 complete; transport remains R3 |
| FFmpeg audio processing | VideoContextService and segmented transcription service | infrastructure/media/audio.py, runner.py, and real FFmpeg validation | PARITY_COMPLETE | Existing FFmpeg adapter | Keep |
| FFmpeg frame extraction | scene-change extraction with 30-second fallback | infrastructure/media/keyframes.py and runner.py | PARITY_COMPLETE | Existing FFmpeg keyframe adapter | Keep |
| ASR | segmented TeleSpeechASR-compatible HTTP path | segmented HTTP ASR adapter and local Whisper adapter; real local tiny.en validation | PARITY_COMPLETE | Python ASR adapter selected by deployment | Keep; provider wiring remains deployment choice |
| OCR | Tesseract frame OCR and perceptual de-duplication | infrastructure/media/ocr.py and hashing.py | PARITY_COMPLETE | Tesseract OCR adapter | Keep |
| Multimodal VideoContext | VideoContextService merges ASR/OCR/frame evidence into timestamped segments | application/context.py VideoContextBuilder over media observations | PARITY_COMPLETE | Existing VideoContextBuilder | Keep |
| Five-minute long-video chunking | VideoChunkingService uses 300,000 ms buckets | application/chunking.py VideoChunkingService | PARITY_COMPLETE | Existing application chunking service | Keep |
| Chunk summary | structured DeepSeek summary with bounded fallback | application chunking plus LocalChunkSummaryAdapter and OpenAI-compatible summary adapter | PARITY_PARTIAL | structured LLM summary with explicit fallback | Wire production summary in R4 |
| Keyword retrieval | keyword scoring in VideoEvidenceRetrievalService | application/retrieval.py lexical/keyword scoring | PARITY_COMPLETE | Existing hybrid retrieval service | Keep |
| Embedding retrieval | EmbeddingUtils with BGE-M3-compatible model and Qdrant search | EmbeddingPort with the canonical OpenAI-compatible BGE-M3 adapter seam, explicit local fallback, and Qdrant production composition; live vectors/search verified | PARITY_COMPLETE | BGE-M3 through EmbeddingPort + Qdrant | R2 storage/retrieval wiring complete; provider E2E remains deployment-configured |
| Qdrant | QdrantVectorStore and Qdrant Compose service | infrastructure/vector/qdrant.py REST adapter in `ProductionR2Services`; live upsert/search and Python recreation recovery verified | PARITY_COMPLETE | QdrantVectorIndex | R2 complete |
| Hybrid Retrieval | semantic, keyword, and visual ranking with bounded hits | application/retrieval.py shared scoring and fallback paths | PARITY_COMPLETE | One VideoEvidenceRetrievalService over Qdrant | Keep algorithm; R4 production path |
| Retrieval Intent / Query Planning | DeepSeek retrieval planner and evidence search controller | retrieval ports, local deterministic planner, OpenAI-compatible retrieval adapter | PARITY_PARTIAL | structured retrieval planner in Agent/application path | R4 production composition |
| Analysis Mode / ModeProfile | AnalysisMode, ModeRegistry, route endpoint, four profiles | domain modes and ModeProfile plus FastAPI `/analysis/route` bounded local router | PARITY_COMPLETE | FastAPI mode routing + ModeProfile | R4 may replace the local classifier composition |
| AgentState | Java AgentState DTO and checkpoints | domain/agent.py AgentState and nested plan/critic models | PARITY_COMPLETE | Existing AgentState | Keep |
| Planner | DeepSeekUtils.plan with structural validation | AgentLoopService and PlannerModelAdapter; real Planner was exercised in prior 10B-FIX | PARITY_COMPLETE | Existing explicit Planner role | Keep |
| Plan Repair | repairPlan/replan after invalid plan or Critic feedback | AgentLoop plan repair and Critic-driven replan | PARITY_COMPLETE | Existing AgentLoop repair boundary | Keep |
| Executor | structured result generation with evidence binding | ExecutorModelAdapter and AgentLoop round execution; real Executor was exercised | PARITY_COMPLETE | Existing explicit Executor role | Keep |
| Executor Checkpoint | checkpoint draft before Critic and resume shortcut | checkpoint_service.py, AgentLoop draft checkpoint and worker recovery over SQLAlchemy/MySQL durable rows with Redis cache | PARITY_COMPLETE | MySQL durable checkpoint + Redis hot state | R2 complete; worker transport remains R3 |
| Critic | structured target/structure/evidence check | CriticModelAdapter, AgentLoop normalization and retry; real Critic was exercised | PARITY_COMPLETE | Existing explicit Critic role | Keep |
| Evidence Verification | timestamp/source/text/claim verification service | application/evidence.py EvidenceVerificationService and Evidence Guard | PARITY_COMPLETE | Authoritative Evidence Guard | Keep |
| Critic-driven targeted retrieval | Critic required timestamps trigger refined context/retrieval | application/long_context.py refine_for_critique and AgentLoop retry | PARITY_COMPLETE | Existing retrieval retry path | Keep |
| Context Budget | bounded UTF-16 context selection | LongVideoContextService 24,000-unit budget | PARITY_COMPLETE | Existing long-context service | Keep |
| Agent Budget | round, duration, token, and cost limits | AgentBudgetConfig, BudgetUsage, execution deadlines, and usage checks | PARITY_COMPLETE | Existing Agent budget policy | Keep |
| Timestamp evidence grounding | EvidenceVerificationService verifies timestamp coverage, source/text containment, and claim binding | EvidenceVerificationService plus the authoritative Evidence Guard enforce half-open VideoContext timestamp coverage and source/claim support | PARITY_COMPLETE | Existing Evidence Guard | Keep; preserve as the original evidence baseline |
| Agent evaluation endpoint | `GET /analysis/agent-evaluation` delegates to AgentEvaluationService | FastAPI endpoint over the canonical application `AgentEvaluationService` | PARITY_COMPLETE | FastAPI endpoint over the canonical evaluation subsystem | Keep baseline; X3 remains separate |
| Original evaluation metrics | AgentEvaluationService returns `structuredValid`, `timestampCoverageRate`, `evidenceSupportRate`, `claimEvidenceSupportRate`, `criticPassed`, `userAcceptanceRate`, and `feedbackSamples` | Single baseline service returns all seven original fields with Java zero-denominator behavior | PARITY_COMPLETE | One original-baseline evaluation service, later extended by X3 | X3 only adds approved enhancement metrics |
| Offline golden evaluation runner | Conditional OfflineAgentEvaluationRunner runs golden tasks, logs metric maps, and applies structured/claim/keyword thresholds | `OfflineAgentEvaluationRunner` application seam runs supplied golden tasks through existing AgentLoop | PARITY_PARTIAL | Canonical offline evaluation runner over the existing AgentLoop | Classpath golden-task loading remains a later integration concern |
| Agent trace endpoint | `GET /analysis/agent-trace` returns AgentTelemetry.latest for a task/goal/mode | FastAPI endpoint over one bounded local baseline trace store | PARITY_COMPLETE | FastAPI endpoint over the canonical trace/telemetry subsystem | R2 adds durable Redis latest lookup |
| AgentTelemetry baseline | UUID trace, task/goal identity, start time, stage durations, call/failure counters, estimated tokens/cost, and arbitrary numeric values; used by AgentLoop, model, media, chunking, and retrieval services | TelemetryPort, TraceContext, and Redis-backed `RedisAgentTelemetry` provide the baseline trace fields plus latest lookup and bounded counters | PARITY_COMPLETE | One canonical AgentTelemetry-compatible service | R2 durable path complete; X2 remains separate |
| Trace Redis persistence/latest lookup | AgentTelemetry persists snapshots and latest-trace indexes in Redis, reads memory then Redis, and retains keys for seven days | `RedisTraceStore` persists snapshots, task/goal/media indexes, latest lookup, and seven-day TTL; live recovery verified | PARITY_COMPLETE | Redis-backed persistence and latest lookup behind the canonical trace service | R2 complete |
| Checkpoint | MySQL durable records with Redis cache for context/chunks/plan/Critic/result | checkpoint service with SQLAlchemy/MySQL durable truth and Redis hot cache; cache wipe recovery live verified | PARITY_COMPLETE | SQLAlchemy/MySQL truth + Redis hot cache | R2 complete |
| Resume | worker and Agent resume from stored lifecycle/result/checkpoint | AgentLoop recovery and TaskWorker recovery semantics; Celery redelivery plus R2 durable checkpoint/result recovery | PARITY_COMPLETE | Celery redelivery + MySQL/Redis checkpoint recovery | R3 live crash/redelivery proof complete |
| MySQL truth | MyBatis-Plus entities/tables and AgentCheckpointRepository | SQLAlchemy 2.x schema/repositories in `ProductionR2Services`; live transaction and restart/recovery verified | PARITY_COMPLETE | SQLAlchemy 2.x + MySQL | R2 complete |
| Redis hot state | RedisTemplate/Redisson for cache, locks, quotas, active markers | Redis cache, auth/session state, markers, token-safe locks, quotas, and telemetry in the R2 composition | PARITY_COMPLETE | Redis | R2 complete |
| Retry | bounded model/ASR retries and async redelivery | provider/media retry policies, existing TaskWorker classification, Celery redelivery mechanics, and bounded dead-letter handoff | PARITY_COMPLETE | Celery delivery mechanics around existing TaskWorker semantics | R3 complete; provider E2E remains R4 |
| Failed/dead-letter behavior | failed task table/topic, manual replay, bounded consumer recovery | existing TaskWorker failure classification, MySQL failed-task ledger, durable pending handoff, and RabbitMQ DLX/DLQ transport | PARITY_COMPLETE | Celery failure/dead-letter integration with MySQL/Redis | R3 complete; full provider replay remains R4 |
| Task lifecycle | TaskStatus, TaskStage, TaskEvent, AnalysisStatusService | domain tasks, TaskLifecycle, TaskStatusProjection, and event delivery ports | PARITY_COMPLETE | One lifecycle/status implementation behind FastAPI/Celery | Keep; adapt transport |
| SSE stages | Spring SseEmitter and TaskEventService | FastAPI SSE over existing TaskEvent/TaskEventDeliveryService/TaskStatusProjection and Redis event history; Celery worker publishes through the same boundary | PARITY_COMPLETE | FastAPI SSE | R3 transport added without changing frames |
| Final result | AnalysisResult with conclusions, evidence, suggestions, Markdown/UI rendering | domain analysis models, guarded result, composition/CLI/Web mapping; real guarded result validated | PARITY_COMPLETE | FastAPI result DTO + Vue renderer | R1/R4 |
| Vue product path | client Vue 3/Vite workbench, upload, task events, evidence, follow-up | `client/` adapted from the public Vue 3/Vite client with FastAPI proxy target | PARITY_COMPLETE | Vue 3 + Vite | Keep; R4 provides full live-provider E2E |
| Follow-up behavior | AnalysisController follow-up, evidence search, feedback/revision on same media | FastAPI follow-up/evidence/feedback/revision endpoints over the same TaskKey semantics | PARITY_COMPLETE | FastAPI follow-up/evidence/feedback endpoints over same task identity | Keep baseline; production provider composition remains later |

## R3 transport boundary and reliability contract

The Python production mapping is deliberately transport-only:

| Original semantic | Python R3 boundary |
| --- | --- |
| RocketMQ enqueue | `CeleryTaskTransport` → RabbitMQ durable main exchange/queue |
| RocketMQ consumer | `dovideo.analysis.deliver` Celery task → existing `TaskWorker` |
| `maxReconsumeTimes=2` | existing inclusive business attempts 1/2/3, scheduled through Celery redelivery |
| RocketMQ dead topic | explicit RabbitMQ DLX/DLQ transport destination plus existing durable failure/handoff state |
| Task event publication | existing `TaskEventDeliveryService`/Redis status history/SSE, not RabbitMQ |

RabbitMQ/Celery is at-least-once transport. Duplicate or redelivered messages
are expected, and correctness comes from the existing active reservation,
token-safe Redis lock, durable MySQL checkpoint/result, idempotent TaskWorker
recovery, bounded business attempts, and recoverable dead-letter handoff.
Exactly-once execution is not claimed. The R3 live smoke proves the success,
transient, permanent, poison, saved-result, and worker-restart boundaries with
real RabbitMQ and a real Celery worker. Full provider-backed long-video E2E
remains R4.

## Approved enhancement assessment

The status column remains APPROVED_ENHANCEMENT so these rows do not inflate
original parity. The original timestamp-evidence, evaluation, and telemetry
rows above remain separate. The current implementation state of each delta is
explicit.

| Approved Enhancement Delta | Current assessment | Current Python evidence | Status | Final Canonical Implementation | Action / Remaining Stage |
| --- | --- | --- | --- | --- | --- |
| Deep Evidence Provenance | PARTIAL | The original timestamp/source/claim baseline is present; source-to-ASR/OCR-to-VideoContext-to-original-video lineage is not yet a first-class record | APPROVED_ENHANCEMENT | answer → evidence → ASR/OCR → VideoContext → timestamp/window → MinIO video | X2 |
| Restricted Tool Calling | COMPLETE | X1-D2 production wiring provides a static three-tool read-only registry, deterministic policy, durable recovery, bounded telemetry, and Evidence Guard preservation | APPROVED_ENHANCEMENT | three-tool allow-listed registry with validation, trusted same-video execution, and durable result recovery | X1 CLOSED |
| Structured Execution Trace / Replay | PARTIAL | TraceContext and telemetry hooks exist, but Planner I/O, retrieval intent/query, retrieved chunks, tool calls/results, Executor/Critic I/O, and evidence-verification detail are not a structured replay record | APPROVED_ENHANCEMENT | Extend the original AgentTelemetry trace with one structured Agent execution chain and replay/inspection view | X2 |
| Advanced Evaluation / Ablation | NOT_STARTED | The original metric baseline is not yet exposed in Python; no Recall@K, Evidence Hit Rate, Groundedness, round/token/cost/latency report, or ablation runner exists | APPROVED_ENHANCEMENT | Extend the original AgentEvaluationService metrics with X3 metrics and bounded ablations | X3 |

## Current-role classification

These rows explain existing code that must not be mistaken for a second
production system.

| Current role | Current Python implementation | Status | Canonical role | Action |
| --- | --- | --- | --- | --- |
| SQLite checkpoint/media store | infrastructure/persistence/sqlite.py and SQLite repositories | TEST_FALLBACK | test/local adapter | Keep and label |
| In-memory cache/upload/object store | infrastructure/persistence/cache.py and infrastructure/media/memory.py | TEST_FALLBACK | test/local adapter | Keep and label |
| In-memory vector index | presentation/composition.py InMemoryVectorIndex | TEST_FALLBACK | test/local developer composition | Keep as explicit test fallback; R2 uses Qdrant |
| Local TF-IDF embedding | infrastructure/providers/local_embedding.py | TEST_FALLBACK | offline/test/fallback embedding | Keep as explicit fallback |
| Standard-library Web Demo | src/dovideo/web.py and web tests | TEMPORARY_DEMO | legacy presentation only | Remove after Vue/FastAPI path is validated in R1 |
| Provider-neutral ports and adapter plumbing | application/ports and infrastructure provider boundaries | INTERNAL_IMPLEMENTATION_DETAIL | preserve one production seam | Keep; do not market separately |
| Status projection and durable dead-letter handoff internals | application/status_projection.py, event_delivery.py, persistence/dead_letter_handoff.py | INTERNAL_IMPLEMENTATION_DETAIL | reliability implementation detail | Keep; no Event Sourcing claim |
| CLI composition and Markdown output | src/dovideo/cli.py and presentation/composition.py | INTERNAL_IMPLEMENTATION_DETAIL | developer/debug utility | Keep; do not present as the main product path |
| React migration alternative | No React files, dependencies, or canonical wiring discovered | OUT_OF_SCOPE | none | Do not add; no cleanup required |

## Matrix count summary

Counts cover 61 rows: 48 original capabilities, 4 approved enhancement deltas,
and 9 current-role classification rows.

| Status | Count |
| --- | ---: |
| PARITY_COMPLETE | 44 |
| PARITY_PARTIAL | 3 |
| PARITY_MISSING | 0 |
| APPROVED_ENHANCEMENT | 4 |
| INTERNAL_IMPLEMENTATION_DETAIL | 3 |
| TEST_FALLBACK | 4 |
| TEMPORARY_DEMO | 1 |
| OUT_OF_SCOPE | 1 |

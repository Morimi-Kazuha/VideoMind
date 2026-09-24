# Java → Python migration matrix

R0 scope note: this file preserves detailed historical contract and phase
evidence. The canonical current scope and remaining roadmap are defined by
docs/SCOPE_LOCK.md, docs/PARITY_MATRIX.md, and docs/REFACTOR_PLAN.md.
Historical phase labels below do not authorize a new phase or a second
production implementation.

Baseline: Java `DOVideo-AI` at `6de943e001f124979893a3d718afa9edb94a4bef`.
“Complete” means the stated phase's contract or use case has been captured and
tested; it does not mean a future provider/adapter implementation exists.

| Java source and type | Python target | Phase 0/1 status | Compatibility note |
| --- | --- | --- | --- |
| `server/src/main/java/com/example/server/dto/VideoContext.java` — `VideoContext` | `src/dovideo/domain/video.py` — `VideoContext` | Complete | `source`, `userGoal`, `segments`; `transcript_text()` retained. |
| Same file — nested `VideoSegment` | `video.py` — `VideoSegment` | Complete | Top-level Python model plus `VideoContext.VideoSegment` alias. |
| `dto/VideoChunk.java` — `VideoChunk` | `video.py` — `VideoChunk` | Complete | Python `start_ms/end_ms` accepts/emits Java `startTime/endTime`; `startMs/endMs` properties retained. |
| Same file — nested `ChunkSummary` | `video.py` — `ChunkSummary` | Complete | Top-level model plus `VideoChunk.ChunkSummary`. |
| `dto/VideoEvidenceHit.java` | `video.py` — `VideoEvidenceHit` | Complete | Nullable text defaults and Java's intentionally permissive time range retained. |
| `dto/VideoRetrievalIntent.java` | `video.py` — `VideoRetrievalIntent` | Complete | Query trim; terms filter, deduplicate, order-preserve, and cap at 16. |
| `dto/AnalysisResult.java` — `AnalysisResult` | `analysis.py` — `AnalysisResult` | Complete | Defaults and `to_markdown()` retained; sections are immutable tuples. |
| Same file — nested `Evidence` | `analysis.py` — `AnalysisEvidence` | Complete | Top-level model plus `AnalysisResult.Evidence`; timestamp is nonnegative. |
| Same file — nested `Section` | `analysis.py` — `AnalysisSection` | Complete | Top-level model plus `AnalysisResult.Section`. |
| `dto/AgentState.java` — `AgentState` | `agent.py` — `AgentState` | Complete | Goal trim/required; round nonnegative; nested aliases retained. |
| Same file — nested `AgentPlan` | `agent.py` — `AgentPlan` | Complete | DTO parsing stays permissive; `is_execution_valid()` exposes AgentLoop 1–5/500 bounds. |
| Same file — nested `CriticResult` | `agent.py` — `CriticResult` | Complete | Nullable lists become tuples; missing primitive `passed` defaults false. |
| `dto/AnalysisMode.java` — `AnalysisMode` | `modes.py` — `AnalysisMode` | Complete | `from_nullable` fallback and strict `from_request` preserved. |
| `service/mode/ModeProfile.java` | `modes.py` — `ModeProfile` | Complete | Profile schema only; registry behavior is future application work. |
| `service/mode/ModeRegistry.java` | Future mode registry/application module | Contract referenced | No service/registry is implemented in Phase 1. |
| `dto/TaskStatus.java` — `TaskStatus` and nested `State` | `tasks.py` — `TaskStatus`, `TaskStatusState` | Complete | Factory methods and AgentState warning result retained; nested `TaskStatus.State` alias. |
| `dto/TaskStage.java` | `tasks.py` — `TaskStage` | Complete | All values retained; unknown/blank `from_value` returns `None` without trimming. |
| `dto/TaskEvent.java` | `tasks.py` — `TaskEvent` | Complete | `of()` and terminal-state semantics retained. |
| `service/AgentLoopService.java` | `src/dovideo/application/agent.py` — `AgentLoopService` | Phase 7 complete | Controlled P-E-C orchestration, checkpoint recovery, evidence/replan retries, provider-reported usage caps, and injectable deadline cancellation are implemented and unified-acceptance tested. |
| `service/LongVideoContextService.java` | `src/dovideo/application/long_context.py` — `LongVideoContextService` | Phase 5C complete | Five-minute bypass, media-scoped optional chunk checkpoint reuse, 24,000 UTF-16 budget, and Critic refinement are implemented; durable persistence remains Phase 8. |
| `service/VideoContextService.java` | `src/dovideo/infrastructure/media/keyframes.py`, `ocr.py`, `orchestration.py` plus `src/dovideo/application/context.py` | Phase 3B + 4 complete | Keyframe/OCR branch observations and pure 60-second `VideoContext` merge are implemented; persistence/checkpointing remains Phase 8. |
| `service/VideoChunkingService.java` | `src/dovideo/application/chunking.py` — `VideoChunkingService` | Phase 5A complete | Async five-minute buckets, stable segment ordering, summary/embedding fallbacks, keyword normalization, and fallback metrics; no retrieval or persistence. |
| `service/VideoEvidenceRetrievalService.java` | `src/dovideo/application/retrieval.py` — `VideoEvidenceRetrievalService` | Phase 5B complete | Top-3 chunk/8-hit weighted hybrid ranking, lexical/cosine fallback, OCR sources, deterministic ordering, and bounded snippets are implemented. |
| `service/QdrantVectorStore.java` | `src/dovideo/infrastructure/vector/qdrant.py` — `QdrantVectorIndex` / `QdrantVectorStore` | Phase 5D complete; offline-tested | Injectable async JSON REST adapter with lazy collection creation, media filter, deterministic Java UUID point IDs, failure reset, and best-effort delete; no live Qdrant test. |
| `service/EvidenceVerificationService.java` | `src/dovideo/application/evidence.py` — `EvidenceVerificationService`, `enforce_evidence_bounds` | Phase 6 complete | Half-open timestamp coverage, ASR/OCR source/text matching, normalized claim binding, and deterministic Critic evidence-bound repairs are implemented; verification is programmatic, not prompt-only. |
| `service/TaskEventService.java` | `src/dovideo/application/event_delivery.py` — `TaskEventDeliveryService` over existing publisher/projection ports | Phase 9D complete; accepted/frozen | Existing `TaskEvent`/publisher shape is reused; delivery is best effort and the in-memory projection exposes current status only. No durable event history or exactly-once claim. |
| `utils/DeepSeekUtils.java` | `src/dovideo/infrastructure/providers/model.py` — `OpenAICompatibleChatClient` plus role adapters | Phase 10A accepted/frozen; live not verified | Java prompt roles and DTO mapping are retained behind the existing Planner/Executor/Critic/ChunkSummary ports; application policy still owns structural/evidence validation. |
| `utils/EmbeddingUtils.java` | `src/dovideo/infrastructure/providers/embedding.py` — `OpenAICompatibleEmbeddingAdapter` | Phase 10A accepted/frozen; Phase 11 live verified | Maps `data[0].embedding` to a finite immutable tuple; blank input remains an empty vector and application retrieval/chunking owns fallback behavior. |
| `utils/AliyunAsrUtils.java`, `utils/OcrUtils.java` | Existing `src/dovideo/infrastructure/media/{asr,ocr}.py` adapters | Phase 3B complete; 10A reused | Existing multipart ASR and Tesseract adapters are reused unchanged; live provider/tool verification remains environment-dependent. |
| `README.md` | `README.md` | Phase 11 productization complete | README documents the CLI, local/remote embedding modes, verified representative result, and explicit deployment limitations. |

## Phase 9D event/status delivery boundary status

| Java source and type | Python target | Phase 9D status | Compatibility note |
| --- | --- | --- | --- |
| `service/TaskEventService.java` — `publishAnalysis`, `publishTranscription`, `publish`, `onMessage`, `send` | `src/dovideo/application/event_delivery.py` — `TaskEventDeliveryService.publish` / `deliver` | Implemented; pending web review | Existing `TaskEventPublisherPort` is the transport-neutral injection boundary. Projection occurs before optional notification; ordinary sink failures are reported as `delivered=False` without changing the projected status. |
| `service/AnalysisStatusService.java` — `current`, `stage`, `statusMessage` | `src/dovideo/application/status.py` — `AnalysisStatusQuery`; `status_projection.py` — `TaskStatusProjection` | Complete and reused | Durable checkpoint/active status query is unchanged; the 9D service exposes the event-side current snapshot and stage through the existing 9C guards. |
| `dto/TaskEvent.java`, `dto/TaskStatus.java`, `dto/TaskStage.java` | `src/dovideo/domain/tasks.py`; `src/dovideo/application/task_lifecycle.py` | Complete and frozen | Existing values, `TaskEvent.terminal()`, `TaskLifecycleEvent`, `TaskKey`, retrying/failed/dead-letter combinations are reused; no V2 model or second state machine. |
| Java Redis notification/local subscriber implementation | Existing `TaskEventPublisherPort` plus caller-provided publisher adapter | Python-side adaptation | Python 9D has no concrete broker or presentation implementation, no event history, and no cross-process replay. `replay` remains explicit caller-supplied in-memory projection input. |

## Phase 10A real media/model provider adapter status

| Java source and type | Python target | Phase 10A status | Compatibility/provider note |
| --- | --- | --- | --- |
| `utils/AliyunAsrUtils.java`, `service/SegmentedTranscriptionService.java` | `infrastructure/media/asr.py` — `HttpAsrAdapter`, `SegmentedTranscriptionService` | Accepted/frozen reuse; deterministic-tested; live not verified | Multipart file/model request, 60-second discovered windows, `[i*60s,(i+1)*60s)` millisecond spans, empty text omission, bounded retry, and cancellation behavior remain frozen from 3B. |
| `utils/OcrUtils.java`, `service/VideoContextService.java` | `infrastructure/media/ocr.py`, `keyframes.py`, `runner.py` | Accepted/frozen reuse; deterministic-tested; live not verified | Tesseract argument vector/language, timestamp order, empty OCR, dHash de-duplication, and process timeout stay below the existing ports. |
| `utils/EmbeddingUtils.java` | `infrastructure/providers/embedding.py` — `OpenAICompatibleEmbeddingAdapter` | Accepted/frozen; fake-tested; Phase 11 live verified | Standard-library/injectable JSON HTTP; OpenAI `data[0].embedding` and direct local vector forms map to finite tuples; no provider SDK leaks into application. |
| `utils/DeepSeekUtils.java` — `plan`, `repairPlan`, `replan`, `execute`, `critique`, `summarizeChunk` | `infrastructure/providers/model.py` — `PlannerModelAdapter`, `ExecutorModelAdapter`, `CriticModelAdapter`, `ChunkSummaryModelAdapter` | Accepted/frozen; fake-tested; live not verified | Existing Java role prompt intent and camelCase DTO aliases are retained; malformed structured output raises a typed adapter error and is not business-repaired. |
| `service/VideoChunkingService.java` — summary/embedding calls | `infrastructure/providers/summary.py` — `LocalChunkSummaryAdapter`; existing `application/chunking.py` | Accepted/frozen reuse; fake-tested | Local summary is deterministic and bounded; the application keeps its existing summary/embedding fallback metrics and hybrid retrieval semantics. |
| `utils/YtDlpUtils.java`, `service/MediaIngestService.java`, `service/QdrantVectorStore.java` | Existing `infrastructure/media/url.py`, `ingest.py`, `infrastructure/vector/qdrant.py` | Reused; offline-tested; live not verified | Local-file ingest and existing URL/Qdrant adapters remain available; no new SDK or live service dependency was introduced. |
| Java `@Value` AI/tool settings | `infrastructure/providers/config.py` — `ProviderConfig.from_environment` | Accepted/frozen; fake/config-tested | Explicit DI or lazy environment factory; endpoint/model/timeout/retry validation is secret-safe and no environment/network work happens at import. |

## Phase 10B real workflow preflight

| Boundary | Current evidence | Status / next requirement |
| --- | --- | --- |
| DeepSeek Planner/Executor/Critic | Java `DeepSeekUtils`; Python 10A OpenAI-compatible role adapters | Blocked before smoke: provide `SILICONFLOW_API_KEY` and endpoint/model, or Python `DOVIDEO_MODEL_API_KEY` and `DOVIDEO_MODEL_BASE_URL`/`DOVIDEO_MODEL`; confirm supported model. |
| Local media toolchain | Existing FFmpeg/ffprobe/Tesseract/yt-dlp adapters | Blocked before real media smoke: provision binaries and a local 30-second–3-minute video with speech/visible text. |
| Phase 10B complete E2E | Existing application ports and AgentLoop | Not started; no fake transcript, embedding, model output, or hardcoded result is accepted as E2E evidence. |

## Phase 11 productization and live embedding status

The preflight table above is retained as historical evidence. The later
Phase 10B-FIX and Phase 11 execution completed the real representative path
and productized its composition root without changing the frozen application
semantics.

| Boundary | Python target | Current evidence | Status |
| --- | --- | --- | --- |
| Java `utils/EmbeddingUtils.java` provider boundary | `OpenAICompatibleEmbeddingAdapter` behind `EmbeddingPort` | SiliconFlow OpenAI-compatible `/v1/embeddings`, model `BAAI/bge-m3`, four finite vectors at dimension 1024 | LIVE VERIFIED |
| Local fallback | `LocalTfidfEmbeddingAdapter` | Deterministic offline adapter and Phase 10B representative vectors | AVAILABLE / VERIFIED |
| Representative retrieval | Existing `VideoEvidenceRetrievalService` | Two chunks, six candidates, rank-1 `300000–360000ms` later After Love region | VERIFIED WITHOUT HARDCODED RETRIEVAL |
| Product entry point | `python -m dovideo analyze` | Single composition root, Markdown evidence output, offline productization tests | IMPLEMENTED |

The original Java configuration uses one normal SiliconFlow API credential
for its OpenAI-compatible model and embedding endpoints; the Python CLI keeps
the embedding credential in a dedicated environment variable for explicit
operator control but does not require a separate provider product or key.

## Phase 2 application boundary status

The following mappings are complete as provider-neutral contracts or a pure
read use case.  They intentionally do not claim persistence, queue, HTTP, or
external model implementations.

| Java source and type | Python target | Phase 2 status | Boundary preserved |
| --- | --- | --- | --- |
| `service/AnalysisStatusService.java` — `current`/`stage` | `src/dovideo/application/status.py` — `AnalysisStatusQuery` | Complete | Terminal result precedence, active queued/processing states, inactive budget/failure/not-started branches, GENERAL default, and Chinese messages. |
| `service/AgentCheckpointService.java` | `src/dovideo/application/ports/checkpoint.py`, `src/dovideo/application/checkpoint_service.py`, and `src/dovideo/infrastructure/persistence/` | Phase 8 complete; accepted/frozen | Existing async application ports remain unchanged; 8A–8D cover durable-first records, media/goal service methods, staged revisions, hot-only feedback, failure markers, media cleanup, and driver-neutral MySQL/Redis/SQLite media adapters; 8E verifies reopen/fault recovery over the Phase 7 loop. Live deployment connections remain unverified. |
| `service/AnalysisDispatchService.java` — `isActive` | `src/dovideo/application/ports/tasks.py` — `TaskActivityPort` | Contract complete | Activity is observed through a normalized `TaskKey`; content/fallback lease details stay in the adapter. |
| `service/AnalysisDispatchService.java` — submission result | `src/dovideo/application/ports/tasks.py` + `value_objects.py` — `TaskDispatchPort`, `DispatchDisposition` | Contract complete | Accepted/rate-limited/duplicate/failed outcomes remain provider-neutral; no queue dispatch occurs. |
| `service/VideoContextService.java`, `SegmentedTranscriptionService`, `OcrUtils` | `src/dovideo/application/ports/media.py`, `ports/ai.py` — `ReadableSourcePort`, low-level frame/audio ports | Contract complete | Readable-source and low-level ASR/OCR seams are provider-neutral; concrete 3B policies live under `infrastructure/media`. |
| `service/VideoChunkingService.java`, `service/VideoEvidenceRetrievalService.java`, `QdrantVectorStore` | `src/dovideo/application/ports/ai.py`, `ports/retrieval.py` — `ChunkSummaryPort`, `EmbeddingPort`, `VectorIndexPort`; `src/dovideo/application/{chunking,retrieval,long_context}.py`; `src/dovideo/infrastructure/vector/qdrant.py` | Phase 5 complete | Typed ports plus 5A chunking, 5B hybrid retrieval, 5C long-context budget/refinement, and 5D Qdrant REST adapter are implemented; durable context/chunk persistence remains Phase 8 and Qdrant is offline-tested only. |
| `service/AgentLoopService.java`, `DeepSeekUtils`, `EmbeddingUtils` | `src/dovideo/application/ports/ai.py` — planner/executor/critic/retrieval/embedding ports; `src/dovideo/application/{agent,evidence,execution_budget,budget_usage}.py` | Contract + Phase 7 complete | Role boundaries, policy/evidence bounds, controlled retries/checkpoints, provider-reported usage caps, and context-local deadline cancellation are explicit and unified-acceptance tested. |
| `service/TaskEventService.java`, `AgentTelemetry`, trace hooks | `src/dovideo/application/ports/tasks.py`, `src/dovideo/application/ports/observability.py` | Contract + Phase 7 complete | Event, telemetry, trace, clock, ID, and `AgentBudgetUsagePort` seams have no Redis/SSE/metrics SDK dependency; `InMemoryAgentBudgetUsage` is test/provider-neutral support. |
| `MediaService.java`, `MinioUtils` | `src/dovideo/application/ports/media.py` — `MediaMetadataPort`, `ObjectStoragePort` | Contract complete | Metadata/object-storage/readable-source boundaries are async; concrete storage remains future work. |
| `README.md` | `README.md` | Phase 2 historical reference; Phase 11 productized | The historical Phase 2 row is retained; the current README documents the implemented CLI, embedding modes, verified representative path, and deferred deployment work. |

## Phase 3A media preprocessing status

This slice is complete for local preprocessing contracts and adapters only.
Object-storage integration and `VideoContext` construction are distinct
boundaries: the provider-neutral 3C ingest/upload slice is covered below,
while the pure `VideoContext` construction is covered by Phase 4 below.  ASR/
OCR observation orchestration is covered by the 3B slice.

| Java source and type | Python target | Phase 3A status | Compatibility/security note |
| --- | --- | --- | --- |
| `service/SegmentedTranscriptionService.java` — `runFfmpeg` | `src/dovideo/infrastructure/media/audio.py` — `AudioSegmenter` | Complete | Retains `libmp3lame`, `segment_time=60`, `reset_timestamps=1`, and `audio_%03d.mp3`; discovers actual files and derives numeric offsets without assuming count. |
| `service/VideoContextService.java` — `extractKeyFrames` | `src/dovideo/infrastructure/media/keyframes.py` — `KeyframeExtractor` | Complete | Retains scene threshold `0.35`, `showinfo`, `-vsync vfr`, and 30-second minimum spacing; adds explicit `fps=1/30` fallback only after a successful empty scene pass. |
| Same file — `PTS_TIME` parsing | `keyframes.py` — timestamp parser | Complete | Captured stdout/stderr are parsed deterministically; missing timestamps use fixed positional offsets. |
| `service/VideoContextService.java` — temporary directory/finally cleanup | `src/dovideo/infrastructure/media/workspace.py` — `MediaWorkspace`, `ScopedArtifact` | Complete | Owned `mkdtemp` scope is removed on success/failure; traversal/symlink escapes are rejected and artifact paths fail after close. |
| `utils/MinioUtils.java` — readable source boundary | `src/dovideo/application/ports/media.py` + scoped artifact references | Boundary only | No object-storage upload/presign implementation; local artifacts never masquerade as durable URLs. |
| `service/MediaIngestService.java`, `service/ChunkUploadService.java` | `src/dovideo/infrastructure/media/ingest.py`, `uploads.py` plus `application/media.py` and `ports/ingest.py` | Phase 3C complete | Provider-neutral streamed direct ingest, 5 MiB/410-part resume, exact ordered merge, TTL, ownership, idempotency marker, rollback, and in-memory test adapters are implemented; no production storage/controller. |
| Local `ffprobe` duration discovery | `src/dovideo/infrastructure/media/ffprobe.py` — `FfprobeDurationAdapter` | Complete | Parses finite non-negative `format.duration`; invalid/missing output maps to `MediaProbeError`. |
| Java `ProcessBuilder` calls | `src/dovideo/infrastructure/media/runner.py` — `AsyncSubprocessRunner` | Complete | Uses argument-vector `create_subprocess_exec`, captures streams, maps non-zero/timeout/launch errors, and kills/reaps timed-out children. |

## Phase 3B segmented ASR/OCR observation status

This slice is complete for provider-neutral observation production and
branch orchestration.  It does not upload media, persist a `VideoContext`, or
perform a live request in the test suite.

| Java source and type | Python target | Phase 3B status | Compatibility note |
| --- | --- | --- | --- |
| `utils/AliyunAsrUtils.java` | `src/dovideo/infrastructure/media/asr.py` — `HttpAsrAdapter` | Complete | Multipart `file`/`model`, Bearer authorization, three-attempt 429/5xx/transport retry with 1/2-second backoff, non-retryable other 4xx, and secret-safe errors. |
| `service/SegmentedTranscriptionService.java` | `asr.py` — `SegmentedTranscriptionService` | Complete | Calls discovered audio files in order; nonblank text becomes `[index*60s,(index+1)*60s)` and partial/all-failure causes are retained. |
| `utils/OcrUtils.java` | `src/dovideo/infrastructure/media/ocr.py` — `TesseractOcrAdapter` | Complete | Uses argument-vector `(image, stdout, -l, chi_sim+eng)`, two-minute timeout, existence check, and trimmed stdout. |
| `service/VideoContextService.java` — dHash/OCR loop | `hashing.py` + `ocr.py` — `PillowDifferenceHash`, `OcrBatchService` | Complete | Java 9x8 grayscale dHash and Hamming `<=5` duplicate rule; empty OCR text still yields evidence; upload failure falls back to `source#timestampMs=N`. |
| `service/VideoContextService.java` — `finishContext` branch handling | `orchestration.py` — `MediaBranchOrchestrator` | Complete | Concurrent branches share one workspace; one failure degrades structurally, both preserve causes and fail, and timeout/cancellation awaits cleanup. |
| `AgentTelemetry` media counters | `telemetry.py` + existing `TelemetryPort` | Complete | In-memory/fake telemetry verifies ASR/OCR calls, per-item failures, evidence upload failures, and branch failures without a metrics SDK. |

## Phase 3C media ingest/upload status

| Java source and type | Python target | Phase 3C status | Deferred behavior |
| --- | --- | --- | --- |
| `service/MediaService.java` — `normalizeVideoFilename`, `calculateMd5`, `saveUploadedMedia` | `src/dovideo/application/media.py`, `src/dovideo/infrastructure/media/ingest.py` | Complete | Java basename/suffix/length rules, bounded MD5 compatibility hashing, object-first then record-save compensation, and safe UUID object keys; MD5 is not a security hash. |
| `service/MediaIngestService.java` — direct file path | `src/dovideo/infrastructure/media/ingest.py` — `MediaIngestService.ingest_file` | Complete | Empty input rejection, bounded streaming, completed immutable `MediaRecord`, and rollback on record failure. |
| `utils/YtDlpUtils.java` / `service/MediaIngestService.java` — URL path | `src/dovideo/infrastructure/media/url.py` — `PublicUrlValidator`, `YtDlpDownloader`; `ingest.py` — `ingest_url` | Complete | HTTP(S)/DNS public-address policy, Java flags, no-shell argv, scoped output cleanup; production DNS-rebinding/redirect egress controls remain deployment work. |
| `service/ChunkUploadService.java` — metadata/parts | `src/dovideo/application/media.py`, `ports/ingest.py`, `uploads.py` | Complete | UUID sessions, 24-hour TTL/renewal, 5 MiB max parts, 410-part cap, owner checks, sorted status, duplicate overwrite, and exact-index completeness. |
| `service/ChunkUploadService.java` — complete/marker/cleanup | `src/dovideo/infrastructure/media/uploads.py` — `ChunkUploadService.complete`; `memory.py` | Complete | Nonblocking lock, ordered streamed merge/MD5, marker-first idempotency, rollback, best-effort cleanup, and test/local TTL-safe adapters; no Redis/MinIO implementation. |
| `utils/MinioUtils.java` — object/part lifecycle | `src/dovideo/application/ports/media.py`, `ports/ingest.py` plus in-memory adapters | Contract + local adapter complete | Port methods and safe object-key semantics are covered; production MinIO, presigned/readable-source, and durable transaction integration remain deferred. |
| `service/MediaController.java` | Future HTTP adapter | Not started | FastAPI/controller/authentication and multipart request envelopes remain outside Phase 3C. |

## Phase 4 VideoContext construction status

| Java source and type | Python target | Phase 4 status | Compatibility note |
| --- | --- | --- | --- |
| `service/VideoContextService.java` — `finishContext`, `merge`, `windowStart`, `SegmentBuilder` | `src/dovideo/application/context.py` — `VideoContextBuilder`, `build_video_context` | Complete | Buckets at `start/timestamp // 60_000 * 60_000`, fixes `end=start+60_000`, joins transcripts in input order, filters blank OCR text, preserves every frame reference, preserves duplicates, and sorts windows ascending. |
| `dto/VideoContext.java` — constructor and `transcriptText()` | `src/dovideo/domain/video.py` — `VideoContext`, `VideoSegment` | Complete | Java source/goal normalization, immutable tuples, validation, transcript aggregation, and stable Java camelCase aliases are reused. |
| `VideoContextService.finishContext` branch-failure/empty checks | `src/dovideo/application/context.py` — `BothObservationBranchesFailed`, `EmptyVideoContextError` | Complete | Both failed branches retain typed causes; one failed branch may degrade; no usable speech/visual evidence is rejected. |
| `AgentCheckpointService` context persistence | `src/dovideo/application/checkpoint_service.py` plus `src/dovideo/infrastructure/persistence/` | Phase 8 complete; accepted/frozen | Durable `(media_id, checkpoint_name)` records, media/goal service methods, revision/failure lifecycle, feedback hot-list, cleanup, driver-neutral persistence adapters, and real AgentLoop recovery integration are implemented; live deployment connections remain unverified. |

## Phase 5 chunking/retrieval/vector status

| Java source and type | Python target | Phase 5 status | Compatibility note |
| --- | --- | --- | --- |
| `service/VideoChunkingService.java` — `build` | `src/dovideo/application/chunking.py` — `VideoChunkingService.build` | 5A complete | Uses 300,000 ms windows from zero through the last start time, stable-sorts segments, skips empty gaps, and returns immutable `VideoChunk` values. Summary fallback is transcript plus normalized OCR truncated to 500 Python characters; embedding failure yields an empty tuple and metrics. |
| `service/VideoEvidenceRetrievalService.java` — retrieval/search/index | `src/dovideo/application/retrieval.py` — `VideoEvidenceRetrievalService` | 5B complete | Top-3 chunks and 8 user hits use Java weights, deterministic lexical/cosine/vector fallbacks, remote range-score overrides, OCR-aware sources, and bounded snippets. |
| `service/LongVideoContextService.java` — select/refine/budget | `src/dovideo/application/long_context.py` — `LongVideoContextService` | 5C complete | Five-minute bypass, optional media-scoped checkpoint reuse, 24,000 UTF-16-code-unit budget, first-oversize admission, overflow continuation, and Critic timestamp refinement are implemented. |
| `service/QdrantVectorStore.java` — collection/upsert/search/delete | `src/dovideo/infrastructure/vector/qdrant.py` — `QdrantVectorIndex`, `QdrantVectorStore` | 5D complete; offline-tested | Async injectable JSON REST boundary plus stdlib client, Cosine collection creation, media filter, Java-compatible name UUID IDs, readiness reset, malformed-response errors, and best-effort delete. No live Qdrant service is tested. |
| `service/AgentCheckpointService.java` — durable context/chunk records | `src/dovideo/application/checkpoint_service.py` plus `src/dovideo/infrastructure/persistence/` | Phase 8 complete; accepted/frozen | Durable records and cache behavior are available offline; 8B service wiring, 8C lifecycle methods, 8D driver-neutral adapters, and 8E AgentLoop recovery integration are implemented. Live deployment connections remain unverified. |

## Phase 8A durable checkpoint repository status

| Java source and type | Python target | Phase 8A status | Compatibility note |
| --- | --- | --- | --- |
| `repository/AgentCheckpointRepository.java`, `mapper/AgentCheckpointMapper.java` | `src/dovideo/infrastructure/persistence/repository.py`, `sqlite.py`, `cache.py`, `ports.py` | 8A implemented; Phase 8 accepted/frozen | Durable reads/writes/deletes use `(media_id, checkpoint_name)` records; cache reads are read-through, durable commits precede cache updates, cache outages are best effort, and stage-only plus payload+stage paths are covered offline. |
| Jackson payloads and checkpoint version policy | `src/dovideo/infrastructure/persistence/codec.py` | 8A implemented; Phase 8 accepted/frozen | Pydantic `by_alias` JSON is wrapped in explicit schema/prompt/embedding versions; missing or mismatched envelopes are not reused. No pickle is used. |
| `service/AgentCheckpointService.java`, `utils/AnalysisTaskKeys.java` | `src/dovideo/application/checkpoint_service.py`, `analysis_task_keys.py` | 8B implemented; Phase 8 accepted/frozen | Async service implements the existing Agent/context/status checkpoint ports over the synchronous 8A repository; Java UTF-8 digest/key namespaces, GENERAL compatibility, mode isolation, cross-goal media context reuse, and typed alias roundtrips are covered offline. |
| `dto/AgentFeedback.java`, staged revision/failure/delete methods in `AgentCheckpointService.java` | `src/dovideo/domain/feedback.py`, `src/dovideo/application/checkpoint_service.py`, `src/dovideo/infrastructure/persistence/cache.py` | 8C implemented; Phase 8 accepted/frozen | UTF-16 bounded immutable feedback normalization, hot-only capped feedback samples, retryable revision checkpoints, durable FAILED stages, and best-effort media/cache cleanup are covered by offline fault-injection tests. |
| MySQL/Redis production deployment adapters and durable media repository | `src/dovideo/infrastructure/persistence/mysql_checkpoint.py`, `redis_cache.py`, `media_repository.py` | 8D implemented; Phase 8 accepted/frozen | Driver-neutral DB-API/redis-py-compatible adapters, SQLite media persistence, and V1 field mappings are offline-tested. No live MySQL/Redis connection is run or claimed; 8E recovery integration is covered by `tests/infrastructure/test_checkpoint_integration_8e.py`. |
| Phase 8E checkpoint recovery acceptance | `tests/infrastructure/test_checkpoint_integration_8e.py` | 8E implemented; Phase 8 accepted/frozen | Real SQLite reopen/fault-injection coverage proves plan/draft/failed-Critic/terminal recovery, durable-before-cache behavior, version invalidation, goal/mode isolation, media context reuse, and concurrent writes. `failed_analysis_tasks` remains Phase 9. |

## Phase 6 evidence verification status

| Java source and type | Python target | Phase 6 status | Compatibility note |
| --- | --- | --- | --- |
| `service/EvidenceVerificationService.java` — `timestampCovered` | `src/dovideo/application/evidence.py` — `EvidenceVerificationService.timestamp_covered` | Complete | Uses any half-open `[startMs,endMs)` context window and returns false for null context/evidence. |
| Same file — `supported` / `supportsClaim` | `evidence.py` — `supported`, `supports_claim` | Complete | Selects ASR, OCR, or combined source text; lowercases and removes Unicode `P*`/`S*`/whitespace before containment/equality checks. |
| `service/AgentLoopService.java` — `enforceEvidenceBounds` | `evidence.py` — `enforce_evidence_bounds` | Complete | Normalizes null/inconsistent Critic results, preserves missing requirements, appends invalid-evidence/unsupported-claim feedback and ordered de-duplicated timestamps; structure bounds remain Phase 7. |

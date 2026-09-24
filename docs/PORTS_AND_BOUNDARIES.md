# Application ports and boundaries (Phases 2–6)

This document freezes the application seam after Phase 0/1 domain contracts.
The Phase 2 use case is `AnalysisStatusQuery`; Phase 3C adds the provider-
neutral media-ingest and resumable-upload contracts.  Tests use in-memory
fakes.  No FastAPI, SQLAlchemy, Redis, Celery, queue client,
LangChain/LangGraph, or production provider is implemented here.

## Dependency rule

```text
domain models/enums  ←  application value objects + ports  ←  use cases
                                                        ↖  adapters (future)
```

Ports import only Phase 1 domain types and the small immutable values in
`application.value_objects`.  They do not import Spring, MyBatis, MinIO,
RocketMQ, Redis, HTTP SDKs, FastAPI, or provider response classes.

All I/O-shaped methods are `async`.  A future adapter may perform blocking
work behind an async boundary (thread/offload policy belongs to that adapter).
Metric increments, clock reads, and ID generation are synchronous because they
are local deterministic hooks rather than network/storage operations.

## Immutable application values

| Value | Purpose and boundary |
| --- | --- |
| `TaskKey(media_id, goal, mode)` | Normalized, hashable analysis identity. Goal is required/trimmed; missing mode is GENERAL. This is the only key passed to status/activity/checkpoint ports. |
| `MediaRef` | Provider-neutral media metadata (`media_id`, source, optional filename/hash/status). It is not a database entity. |
| `ReadableSource` | Source token resolved for an ASR/OCR adapter. It does not promise a local path or a URL scheme. |
| `AnalysisRequest` | Dispatch input that owns a `MediaRef`, normalized goal/mode, and optional request ID; derives a `TaskKey`. |
| `TranscriptSpan` | ASR observation with Java-compatible nonnegative `start_ms < end_ms` and trimmed text. |
| `OcrObservation` | One timestamped OCR observation plus optional frame reference. |
| `VectorHit` | Provider-neutral vector hit (`start_ms`, `end_ms`, score). |
| `TraceContext` | Opaque trace ID tied to a `TaskKey`; no logging/trace SDK leaks through the seam. |
| `DispatchDisposition` | The four Java `AnalysisDispatchService.SubmissionResult` values. |

All values are frozen, slotted dataclasses.  They are intentionally not
duplicates of `VideoContext`, `VideoSegment`, `AnalysisResult`, or other
domain models.

## Ports

### Media boundaries — `application.ports.media`

| Port | Java evidence | Methods / async rationale |
| --- | --- | --- |
| `MediaMetadataPort` | `MediaService.requireOwnedMedia`, `exists`, `contentHash`, and `MediaFile` use in `AnalysisDispatchService` | `get_media(media_id) -> MediaRef | None` is async because the future implementation reads persistence. |
| `ObjectStoragePort` | `MediaService`/`MinioUtils` upload, readable source, and cleanup paths | `put_object(AsyncIterable[bytes], ...)` and `delete_object(source)` are async object I/O. |
| `ReadableSourcePort` | `MediaService.readableSource` and `VideoContextService.build` | `resolve_readable_source(source)` isolates URL/object-to-readable-source resolution and is async for object-store/network access. |

Metadata lookup does not authorize ownership; the eventual application
boundary must perform that policy before exposing a `MediaRef`.  Object
deletion is a cleanup operation and must be idempotent for retry/rollback.

### ASR/OCR boundaries — `application.ports.ai`

| Port | Java evidence | Methods / boundary |
| --- | --- | --- |
| `TranscriptionPort` (`AsrPort` alias) | `SegmentedTranscriptionService` and `VideoContextService` | `transcribe(ReadableSource, trace_id) -> tuple[TranscriptSpan, ...]`; external ASR is async. |
| `OcrPort` | `OcrUtils` and `VideoContextService.extractKeyFrames` | `recognize(ReadableSource, timestamp_ms, trace_id) -> OcrObservation`; one frame call is async. |

The application ports remain deliberately low-level and do not decide branch
fallback, 60-second ASR windows, 30-second visual fallback, perceptual
de-duplication, or evidence-frame cleanup.  Those policies are implemented by
the 3B infrastructure services below and remain separate from Phase 4
`VideoContext` construction.

### LLM role boundaries — `application.ports.ai`

`PlannerPort` (plan/repair/replan), `ExecutorPort`, `CriticPort`,
`RetrievalPlannerPort`, and `ChunkSummaryPort` correspond to the separate
structured calls in `DeepSeekUtils` and the roles in `AgentLoopService`.
Each has only its role's inputs/outputs and all calls are async.  The
`EmbeddingPort.embed(text)` boundary corresponds to `EmbeddingUtils.embed`.

These ports do not contain prompts, retries, budgets, evidence verification,
or tool calling.  The future AgentLoop (Phase 7) owns orchestration and the
future provider adapter owns SDK/request details.

### Retrieval — `application.ports.retrieval`

`VectorIndexPort.upsert`, `.search`, and `.delete_media` correspond to
`QdrantVectorStore`.  Calls are async because a vector index is remote I/O.
The port returns `VectorHit` values and accepts domain `VideoChunk` values, not
Qdrant payloads.  `VideoEvidenceRetrievalService` owns lexical/cosine/vector
fallback and ranking policy; `LongVideoContextService` owns context budgeting
and optional media-scoped chunk checkpoint reuse.  The concrete
`infrastructure.vector.QdrantVectorIndex` uses an injectable async JSON HTTP
client (or the stdlib client) and does not make a request at import or
construction time.

### Checkpoints — `application.ports.checkpoint`

The Java `AgentCheckpointService` exposes content-level context/chunk methods
and goal-level plan/draft/Critic/result/stage methods.  They are split into
small protocols:

- `AnalysisStatusCheckpointPort`: only `load_result(TaskKey)` and
  `load_stage(TaskKey)`; this is the exact read surface needed by
  `AnalysisStatusQuery`.
- `ContextCheckpointPort`: context/chunk load/save, keyed only by media ID so
  video-derived assets can be reused across goals.
- `AgentCheckpointPort`: plan, execution draft, Critic state, and final result,
  keyed by `TaskKey` so modes/goals cannot overwrite each other.

Checkpoint writes must be atomic with their stage update in the eventual
repository.  Result writes are goal/mode scoped; context/chunk writes are
media/content scoped.  Revision and feedback checkpoint methods from Java are
reserved for a later application slice rather than inflating this phase's
interfaces.

### Activity, dispatch, and events — `application.ports.tasks`

`TaskActivityPort.is_active(TaskKey)` captures exactly
`AnalysisDispatchService.isActive`: observe the active lease/idempotency
marker, including content-scope fallback inside the future adapter.  It does
not expose Redis keys or lease TTLs.

`TaskDispatchPort.dispatch(AnalysisRequest)` returns
`DispatchDisposition` (`ACCEPTED`, `RATE_LIMITED`, `DUPLICATE`, `FAILED`),
matching Java's dispatch result without exposing RocketMQ.  Reservation and
release must be idempotent around a future queue transaction; no dispatch is
performed in Phase 2.

`TaskEventPublisherPort.publish(TaskKey, TaskEvent)` carries the domain event
without exposing Redis/SSE.  Event publication is best-effort only after a
future durable dispatch decision; terminal event de-duplication belongs to the
worker/event adapter.

### Telemetry, trace, clock, and IDs — `application.ports.observability`

`TelemetryPort` covers the metric hooks used by `AgentTelemetry` and remains a
small synchronous counter/observation surface.  `TracePort` covers trace
start/annotate/finish and is async so a future durable trace sink does not leak
into use cases.  `ClockPort` supplies wall/monotonic time for deterministic
budgets; `IdPort` supplies opaque IDs instead of hard-coding UUID generation.
No trace sink, metrics backend, or clock implementation is included yet.

## Implemented use case: `AnalysisStatusQuery`

`current(media_id, goal, mode=GENERAL)` builds a normalized `TaskKey`, then
performs the Java-compatible precedence:

1. Load result.  If an `AgentState` exists and contains a result, return
   `TaskStatus.completed(state)` immediately.  This preserves the warning
   markdown when Critic is absent or failed, and terminal result wins even if
   activity/stage markers are stale.
2. Load stage.  If active and stage is absent, return `QUEUED` / `任务已排队`;
   if active and stage exists, return `PROCESSING` with the stage message.
3. If inactive and stage is `BUDGET_EXHAUSTED`, return `FAILED` with
   `Agent 已达到本次任务预算，请调整目标后重试`.
4. If inactive and stage is `FAILED` or `DEAD_LETTERED`, return `FAILED` with
   `分析失败，请稍后重试`.
5. Otherwise return `NOT_STARTED` / `尚未提交分析任务`.

The stage message table preserves the Java mappings for context, chunks,
Planner, Executor, Critic, evidence refresh, retry, and generic stages.  The
use case has no side effects and is fully runnable with in-memory async fakes.

## Explicitly not implemented

The completed Phase 2–6 slices do not implement the full Planner–Executor–Critic
loop, Redis/RocketMQ/SSE, FastAPI, authentication,
Celery, or production provider wiring.  Phase 3C storage remains in-memory/
test-only, and the Qdrant adapter is offline-tested with fake HTTP clients;
no live vector service is contacted by the suite.  Durable media/context/chunk
records and checkpoint persistence remain Phase 8.  The tables above and the
Phase 3C/4/5 contracts document the boundaries future implementations must
honor.

## Phase 3A — local media preprocessing boundary

`src/dovideo/infrastructure/media/` is the first concrete adapter slice.  It
is deliberately not imported by `domain` or `application`.

### Security contract

- `AsyncSubprocessRunner` accepts a non-empty argument sequence and calls
  `asyncio.create_subprocess_exec`; no shell, command string concatenation, or
  shell interpolation is used.  NUL-containing arguments are rejected.
- stdout and stderr are captured separately.  Non-zero exit codes raise
  `SubprocessExecutionError` with command and captured diagnostics; launch
  errors and timeouts have distinct project exceptions.
- Every process has a completion timeout.  Timeout and task cancellation kill
  the child and await its process handle before returning control, preventing
  orphaned ffmpeg/ffprobe processes.
- Output names are adapter-owned fixed patterns (`audio_%03d.mp3`,
  `frame_%06d.jpg`).  Workspace resolution rejects absolute/traversal paths
  and symlink escapes; untrusted names are never used as command fragments.

### Artifact and cleanup contract

- `MediaWorkspace` owns one `tempfile.mkdtemp` child and removes that child on
  both successful and exceptional async-context exits.  A caller-supplied
  parent is never recursively removed.  Re-requesting the same child
  directory is idempotent; a file with that name remains an error.
- `AudioSegment` and `Keyframe` are frozen descriptions with deterministic
  numeric ordering.  Audio offsets are `output_index * 60_000` milliseconds;
  the segment count comes only from discovered files, not a duration estimate.
- Keyframes first use the Java filter
  `select=eq(n\\,0)+gt(scene\\,0.35)+gte(t-prev_selected_t\\,30),showinfo`.
  If that successful pass yields no files, a separate `fps=1/30,showinfo`
  pass supplies the fixed 30-second fallback.  `showinfo` timestamps are
  parsed from captured process output with deterministic fallback offsets.
- Artifact descriptors resolve paths only while their workspace is active;
  accessing `.path` after cleanup raises `WorkspaceClosedError`.  Callers
  needing ASR/OCR consumption must keep the explicit async context open.

### Java mapping and intentional boundary

`SegmentedTranscriptionService.runFfmpeg` maps to `AudioSegmenter`; its
60-second segment flags and `audio_%03d.mp3` naming are retained, while the
Python runner captures diagnostics and reaps timed-out children.  The
`VideoContextService.extractKeyFrames` command and `PTS_TIME` parsing map to
`KeyframeExtractor`; Python separates the empty-result 30-second fallback for
testability.  `VideoContextService`/`MinioUtils` temporary-directory cleanup
maps to `MediaWorkspace`, with scoped artifact references instead of returning
paths after deletion.  `ffprobe` duration is a new small adapter seam for
preprocessing validation.

## Phase 3B — segmented observations and branch orchestration

The 3B implementation is in `src/dovideo/infrastructure/media/` and consumes
3A artifacts without importing infrastructure into `domain` or
`application`:

- `SegmentedTranscriptionService` invokes the low-level audio transcription
  port sequentially, derives 60-second timestamp spans from discovered
  segment indexes, records per-segment causes, and raises a structured
  all-failed error only when no usable span remains.
- `HttpAsrAdapter` sends a multipart `file` plus `model` and a Bearer header.
  429, 5xx, and transport errors receive at most three attempts with injected
  1/2-second backoff; other 4xx responses are immediate failures.  Error text
  never includes the API key, response body, or request headers.
- `TesseractOcrAdapter` invokes the existing argument-vector subprocess port
  as `(image, stdout, -l, chi_sim+eng)` with a two-minute timeout.  The batch
  service trims returned text, records empty text as a valid observation,
  applies Java's 9x8 grayscale dHash with a Hamming threshold of 5, and
  preserves source/timestamp references when evidence persistence fails.
- `MediaBranchOrchestrator` runs audio/ASR and keyframe/OCR concurrently in
  one `MediaWorkspace`.  Branch outcomes are immutable and distinguish
  success, partial failure, and failure while retaining exception objects and
  attempted counts.  Per-item `causes`/`failed` counts exclude a separate
  `branch_error` (for example, extraction failing before any item attempt), so
  counters remain factual.  One failed branch degrades the bundle; two failed
  branches raise `BothMediaBranchesFailed`.  Timeout or child cancellation
  cancels and awaits all children before workspace cleanup; cancellation and
  other `BaseException` control-flow signals are never converted to FAILED.

Telemetry is limited to the existing synchronous `TelemetryPort` seam and
records `asrCalls`, `asrSegmentFailures`, `ocrCalls`, `ocrFrameFailures`,
`frameUploadFailures`, and branch-failure counters.  `PillowDifferenceHash`
is the only image decoding dependency; the application ports remain Pillow-
free.  The adapters are provider-neutral and tested with fakes, so no live
ASR/OCR request occurs in the test suite.  Durable evidence/object storage,
multipart ingest/chunk upload (3C), and `VideoContext` merging (Phase 4) are
outside this boundary.

## Phase 3C — media ingest and resumable upload boundary

The 3C application contracts live in `application.media` and
`application.ports.ingest`; concrete offline implementations live under
`infrastructure.media`.  Application code does not import MinIO, Redis,
SQLAlchemy, FastAPI, yt-dlp, or an HTTP client.

| Port/value | Java dependency and responsibility | Async/transaction boundary |
| --- | --- | --- |
| `MediaRecordPort` / `MediaRecord` | `MediaService.saveUploadedMedia`, `MediaFile` | `save/get/delete` are async persistence calls.  The use case uploads the object first and compensates with delete if record save fails; record and object are not one distributed transaction. |
| `ObjectStoragePort` | `MinioUtils.upload/remove` and direct `MediaIngestService` | Async bounded stream upload/delete.  Object names are generated by the use case and contain only a random UUID plus a validated suffix. |
| `ChunkObjectPort` | `ChunkUploadService.uploadChunk`, `MinioUtils` part objects | Async bounded put/read/delete/list.  Parts are independently idempotent and may remain after a failed complete for retry. |
| `UploadSessionPort` / `UploadSession` | `ChunkUploadService` metadata and `completed:{uploadId}` marker | Async short-lived state.  Sessions and markers expire after 24 hours; each accepted part renews the active session.  Marker write is the completion commit point. |
| `MergeLockPort` | Redisson `tryLock()` per upload | Synchronous nonblocking acquisition returns a lease or conflict; the lease is always released by the caller.  It prevents duplicate merges but is not a database transaction. |
| `UrlDownloadPort` / `DnsResolverPort` | `YtDlpUtils.download` and URL validation | Async download/resolution into a caller-owned `MediaWorkspace`; resolver work is offloaded by the system adapter.  Production must enforce egress policy at the network boundary because DNS can rebind and redirects can change hosts. |

### 3C invariants and lifecycle

- `normalize_video_filename` replaces backslashes with slashes, trims the
  basename, accepts only `.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`, or `.m4v`
  case-insensitively, and enforces 1–255 characters.  The original spelling
  is retained for the display filename; generated object keys never contain
  caller path components.
- Direct files are streamed (the compatibility MD5 is computed in the same
  pass using bounded reads); empty input is rejected before storage.  MD5 is a
  Java compatibility identifier, not an authenticity/security hash.
- A session has 1–410 chunks, each non-empty and at most 5 MiB.  Chunk status
  is a sorted immutable tuple.  Re-uploading an index overwrites that part
  and renews its session without creating a duplicate index.
- Complete requires the exact set `0..total_chunks-1`.  The merger reads in
  numeric order into a scoped temporary file, computes MD5, uploads one media
  object, saves a completed record, and then writes a 24-hour marker.  Only
  after the marker succeeds are part/session cleanup operations attempted;
  cleanup errors do not undo the durable success.  Object/record rollback is
  attempted when upload or marker persistence fails, preserving the original
  exception and attaching cleanup failures as notes.
- A completed marker contains the original filename, part count, and creation
  time so a status response does not invent metadata after active state is
  deleted.  A legacy/corrupt marker lacking that shape is discarded rather
  than fabricated into a false status.
- `InMemoryObjectStorage`, `InMemoryChunkObjectStore`,
  `InMemoryUploadSessionStore`, `InMemoryMediaRecordStore`, and
  `InMemoryMergeLock` are test/local adapters only.  They provide deterministic
  TTL/failure/concurrency behavior and make no production durability or
  security claim.

### 3C intentionally not implemented

There is no MinIO/Redis/SQLAlchemy adapter, multipart HTTP controller,
production yt-dlp downloader deployment, authenticated request middleware,
DNS-rebinding/redirect egress proxy, or durable transaction coordinator in
this phase.  These are integration/deployment work after the contracts are
accepted; Phase 4 `VideoContext` merging is implemented in memory, while
durable context/checkpoint persistence remains Phase 8.

## Phase 4 — pure VideoContext construction boundary

`application.context.VideoContextBuilder` consumes only the immutable
`MediaObservationBundle` from Phase 3B and returns the domain `VideoContext`.
It has no infrastructure/provider import and no persistence side effect.

- Each ASR start time and OCR timestamp is assigned to
  `floor(value / 60_000) * 60_000`; every output segment ends exactly 60,000 ms
  after its start, and windows are returned in ascending order.
- Transcript strings are joined with newline in each branch's input order.
  OCR strings that are blank are omitted, while every non-empty successful
  frame reference is retained even when its OCR text is blank.  Duplicate
  observations/references are intentionally preserved to match Java.
- An ASR-only or OCR-only bundle is valid when it has usable evidence.  Both
  failed branches raise `BothObservationBranchesFailed` with branch-owned
  typed causes.  No usable speech, OCR text, or frame reference raises
  `EmptyVideoContextError`.
- The returned Pydantic domain model performs Java-compatible source/goal
  normalization, immutable defensive collection copying, and stable
  camelCase JSON serialization.  The builder does not write a database or
  checkpoint; that responsibility is explicitly Phase 8.

## Phase 5 — chunking, retrieval, long-context, and vector boundaries

Phase 5 is complete as four application/infrastructure slices.  The
application services depend only on typed ports; Qdrant is isolated under
`infrastructure.vector` and is never imported by domain or application code.

### 5A — five-minute chunk construction

`application.chunking.VideoChunkingService` depends on `ChunkSummaryPort`,
`EmbeddingPort`, and `TelemetryPort`.  It copies and stably sorts segments into
300,000 ms buckets, skips empty gaps, and returns immutable chunks.  A summary
failure uses transcript plus trimmed/nonblank/distinct OCR, joined with spaces
and truncated to 500 Python characters (`summaryFallbacks`).  Keywords are
normalized in order; embedding input is exactly
`summary + "\\n" + " ".join(keywords)`, and embedding failure returns an empty
tuple (`embeddingFallbacks`).

### 5B — hybrid evidence retrieval

`application.retrieval.VideoEvidenceRetrievalService` depends on
`RetrievalPlannerPort`, `EmbeddingPort`, `VectorIndexPort`, and
`TelemetryPort`.  It ranks at most three chunks and eight user hits using
semantic/keyword/visual weights `.60/.25/.15` and segment weights
`.55/.25/.20`.  Blank planner intent falls back to deterministic terms;
embedding and vector failures fall back to lexical/cosine scoring and emit
their metrics.  Remote vector scores override matching ranges; OCR-only
evidence, source labels, ordering, and 180-character snippets are preserved.

### 5C — long-context selection and Critic refinement

`application.long_context.LongVideoContextService` uses
`VideoChunkingService`, `VideoEvidenceRetrievalService`, optional
`ContextCheckpointPort`, and `TelemetryPort`.  Contexts at or below five
minutes bypass retrieval.  Media-scoped cached chunks may be reused; misses
build, save, and best-effort index chunks.  Budgeting counts Java UTF-16 code
units across transcript/OCR, admits the first oversized candidate, skips later
overflow candidates with continuation, and records drop/character metrics.
Critic refinement adds timestamp-margin candidates, ordered range de-duplication,
retry selection, and retains the original context source/goal.

### 5D — Qdrant REST adapter

`infrastructure.vector.QdrantVectorIndex` implements `VectorIndexPort` via a
small injectable async JSON client; its default client wraps stdlib `urllib` in
`asyncio.to_thread`.  It lazily GETs a validated collection, creates Cosine
vectors on 404, upserts only non-empty embeddings, searches with a media-ID
filter, parses payload ranges, and best-effort deletes media points.  IDs are
Java-compatible MD5/name UUIDs from `mediaId:start:end`.  Non-success,
transport, malformed-response, and dimension errors are typed; readiness is
reset after lookup/write/search failures, API keys are excluded from
diagnostics, and cancellation propagates.  Disabled mode performs no requests.

The adapter and all Phase 5 services are tested offline with fakes; no live
Qdrant service is required or contacted.  These boundaries do not provide
durable context/chunk persistence: `ContextCheckpointPort` is optional and
durable repositories remain Phase 8 work.

## Phase 6 — programmatic evidence verification boundary

`application.evidence.EvidenceVerificationService` is a pure service with no
provider, network, persistence, or infrastructure dependency.  Its
`timestamp_covered` check uses the Java half-open interval
`start_ms <= timestamp < end_ms`.  `supported` validates an ASR/OCR source,
selects transcript/OCR/combined text from every covering segment, and checks
normalized containment.  `supports_claim` additionally requires normalized
claim equality.  Normalization is locale-stable lowercase followed by removal
of Unicode punctuation (`P*`), symbols (`S*`), and whitespace using
`unicodedata`.

`enforce_evidence_bounds` is the application collaborator for Java's
`AgentLoopService.enforceEvidenceBounds`.  It converts a missing Critic result
to `Critic 未返回有效结果`, forces passed-with-problems to failed, gives an
empty failed result the generic repair feedback, and then appends unsupported
conclusions and invalid-evidence messages in source order.  Invalid timestamps
are appended once in order; existing feedback, missing requirements, and
unsupported claims are preserved.  Structure validation and complete
Planner–Executor–Critic orchestration remain Phase 7.

The returned Critic/domain collections are immutable tuples and input objects
are not modified.  Offline tests cover all source modes, boundary and Unicode
normalization rules, fabricated evidence, claim binding, Critic aliases and
repair ordering.  This is an executable verification contract; evidence
verification is not represented as a prompt-only instruction.

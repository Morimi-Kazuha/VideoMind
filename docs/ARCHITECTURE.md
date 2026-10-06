# VideoMind architecture and authority boundaries

This document follows one analysis from upload to replay. The same
application/domain behavior is composed behind FastAPI, a Celery worker, and
the smaller CLI path. Adapters provide I/O; they do not define separate Agent
or evidence policy.

## Resumable upload

The browser sends 5 MiB logical chunks with concurrency 3 (at most 410 chunks).
It keeps only a user-scoped UUID upload credential locally; server-confirmed
indexes determine resume progress. Network errors, HTTP 408/429/5xx have at most
four attempts with exponential backoff and jitter. Permanent 4xx are not retried.

Production uses Redis `upload:session:{uuid}` metadata and
`upload:parts:{uuid}` Set, both renewed to approximately 24 hours. MinIO stores
`chunk-uploads/{uuid}/part-{index}` bytes. Each chunk is persisted first, then a
guarded Redis Lua operation records its index and renews both keys atomically.
The operation refuses expired or completed sessions and cannot resurrect them.
An uncertain Redis confirmation leaves the object available for deterministic
re-upload. Pre-Set sessions get one guarded object-list migration; fresh
sessions never count unconfirmed MinIO objects as progress.

Completion validates ownership before acquiring the nonblocking per-upload
`lock:upload-merge:{uuid}` redis-py Lock, then rechecks the completion receipt
under that lock and verifies the exact index set `0..totalChunks-1`. Independent
uploads can merge concurrently. Ordered local workspace merge computes MD5
incrementally while bytes flow; this is a content fingerprint, not an upload ID
or pre-upload deduplication key.

The Redis lock has a 30-minute finite lease; the merge scope has a 20-minute
deadline. The owning token is renewed through the library's `reacquire()` at
chunk and persistence boundaries. Redis calls run outside the event loop;
MySQL connection/read/write I/O is bounded. A cancelled DB save is allowed to
settle before release because cancelling an asyncio await cannot cancel its
executor-thread transaction. There is no periodic watchdog. See
[redis-py Lock](https://redis.readthedocs.io/en/stable/lock.html).

The final object key is `media/upload-{uuid}{suffix}`. MySQL stores its durable
MediaRecord. Redis `upload:completed:{uuid}` retains the media ID, owner and
session shape for approximately 24 hours after completion. **The lock prevents
concurrent merges; the receipt provides business idempotency for later/lost
response retries.** If the durable row committed but receipt creation failed,
complete returns a retryable error and retains row/object/chunks. A retry under
the lock finds the same durable row by its exact deterministic source, writes
the receipt and returns the same media ID without another merge or insert.
Both receipt owner and durable row owner are checked.

Only after receipt success is temporary cleanup attempted, with its own
10-second budget outside the merge deadline. Cleanup failure does not change
the business result. Redis loss/expiry ends the coordination recovery window;
process pauses/failover beyond a lease and orphan objects are explicit limits.
This is not an exactly-once or distributed transaction guarantee. No SQL
per-chunk writes, content deduplication, instant upload, dynamic chunks, Java
lock client or server-side object compose are used. Local development keeps
its explicit process-local upload adapter; it is not a production fallback.

See [source audit](UPLOAD_FINALIZATION_AUDIT.md) and
[validation and failure semantics](UPLOAD_FINALIZATION_REPORT.md).

## Request and preparation lifecycle

1. The Vue client uploads media in bounded chunks and submits an analysis
   goal. FastAPI returns a task identity rather than holding the request open
   for extraction and model work. REST and SSE expose status and results.
2. Task lifecycle and idempotency checks limit duplicate work. Celery and
   RabbitMQ deliver long-running jobs. MySQL holds durable media, task,
   checkpoint, and execution records; Redis is used for hot state, locks,
   projections, and rate limits. MinIO holds media objects.
3. FFmpeg segments audio and extracts keyframes. Whisper ASR and Tesseract
   OCR produce observations with source times. `VideoContextBuilder` merges
   them into ordered temporal windows, preserving modality and origin.
4. Long-video preparation groups windows into five-minute chunks while
   retaining source spans. Embeddings and keywords support hybrid retrieval;
   Qdrant indexes vectors. Retrieval returns candidate evidence, not an
   authoritative answer.
5. `AgentLoop` resolves the model lane once, builds a bounded plan, executes
   structured rounds, invokes Critic, and can retrieve targeted evidence
   when feedback identifies a gap. `EvidenceVerificationService` checks final
   claim text, source identity, and timestamp coverage before a structured
   result is accepted.

The CLI can use local TF-IDF and process-local stores for development and
offline tests. That choice is explicit; it does not silently replace the
production SQL/Redis/MinIO/Qdrant composition. The standard-library web
demo is a legacy local presentation adapter; Vue/FastAPI is the product path.

## Why the boundaries matter

| Boundary | Owner and reason |
| --- | --- |
| Model output → plan | Plan validation enforces task shape and budgets; natural-language instructions alone do not authorize execution. |
| Retrieval → answer | Retrieval ranks likely source material. Evidence Guard independently validates the answer's cited source text and covered time range. |
| Model output → tools | `AgentLoop` and `ToolPolicy` authorize registered, same-video, read-only evidence tools. Requests have stable IDs, bounds, and recovery records; the model has no arbitrary computer-control authority. |
| Jev advice → execution lane | Jev can suggest FAST, BALANCED, or DEEP. VideoMind's deterministic threshold and fallback policy records the final lane and model once per Agent execution. |
| Durable record → replay | The durable execution record is historical truth. A checkpoint is recovery state and Redis is an operational projection. Replay reads the record without provider, retrieval, tool, or Jev calls. |
| Provider → application | OpenAI-compatible chat and embedding adapters translate requests and telemetry. The application consumes typed decisions and usage without granting providers policy authority. |

## Provenance from video to claim

The prepared source revision hashes normalized observations and a versioned
extraction contract, not a temporary input path. Segment and source-item IDs
derive from that revision. Final evidence keeps the source item, temporal
range, and original media identity needed to inspect the cited video range.

Evaluation exposed a concrete bug: an OCR frame reference included an
ephemeral extraction path. The same video prepared in two workspaces could
then produce different provenance IDs. `x2-a-v2` replaced that path with a
stable frame identity; `x2-a-v3` retained finer Whisper segment spans rather
than only coarse audio-file intervals. Historical records and
`golden-dataset-v1` were left untouched. `golden-dataset-v2` uses the newer
source revision and documents its evidence rebinding.

See [OCR provenance](PROVENANCE_V2.md), [ASR granularity](ASR_GRANULARITY_V3.md),
and the [dataset rebase record](../datasets/x3/DATASET_V2_REBASE.md).

## Routing and measured limits

The X3 benchmark froze a heterogeneous three-lane policy: FAST and BALANCED
used DeepSeek Flash with different reasoning settings; DEEP used DeepSeek
Pro. Model execution used OpenRouter with one pinned serving provider and no
fallback. Jev was a separate advisory call, and the VideoMind route record
remained the final authority.

The completed 80-result campaign did not establish a routing benefit. All
16 fixed-lane oracle entries reported no lane passing the preregistered
quality gate; both router arms executed BALANCED for all 16 cases. This is a
bounded negative finding under the specific dataset, provider, one-trial
design, and lexical gate. The [final evaluation report](X3_OPENROUTER_X3_E_REPORT.md)
separates measured cases, derived paired comparisons, incomplete costs, and
threats to validity.

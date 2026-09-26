# DOVideo architecture and authority boundaries

This document follows one analysis from upload to replay. The same
application/domain behavior is composed behind FastAPI, a Celery worker, and
the smaller CLI path. Adapters provide I/O; they do not define separate Agent
or evidence policy.

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
| Jev advice → execution lane | Jev can suggest FAST, BALANCED, or DEEP. DOVideo's deterministic threshold and fallback policy records the final lane and model once per Agent execution. |
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
fallback. Jev was a separate advisory call, and the DOVideo route record
remained the final authority.

The completed 80-result campaign did not establish a routing benefit. All
16 fixed-lane oracle entries reported no lane passing the preregistered
quality gate; both router arms executed BALANCED for all 16 cases. This is a
bounded negative finding under the specific dataset, provider, one-trial
design, and lexical gate. The [final evaluation report](X3_OPENROUTER_X3_E_REPORT.md)
separates measured cases, derived paired comparisons, incomplete costs, and
threats to validity.

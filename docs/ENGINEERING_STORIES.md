# Engineering stories

R0 scope note: the standard-library Web Demo described below is a temporary
presentation adapter, not the canonical product frontend. The final product
direction is Vue 3 + Vite with FastAPI and SSE. The CLI remains a
developer/debug utility. Original parity also includes timestamp evidence,
Agent Evaluation, and AgentTelemetry/Trace baselines; the deeper provenance,
structured replay, and advanced evaluation work is additive X2/X3 scope.

## 1. Preserving the Java contract while changing the runtime

The reconstruction keeps Java-compatible JSON aliases and immutable domain
models, but moves I/O behind Python protocols. That made it possible to test
window construction, chunking, retrieval, policy, and checkpoints with fakes
without weakening the production adapter boundary.

## 2. Long video means time is part of the data

The pipeline does not flatten a transcript into one string. ASR/OCR
observations become fixed temporal windows, chunks retain their source ranges,
and retrieval returns directly seekable evidence. This is why the Agent can
answer a later-region question without a fixed timestamp selection.

## 3. Retrieval is not the Evidence Guard

Hybrid retrieval proposes candidate context. The existing AgentLoop and
`EvidenceVerificationService` still validate source, content, claim, and
timestamp coverage. A top-ranked vector is useful evidence, not permission to
invent a conclusion.

## 4. Local fallback is explicit

`LocalTfidfEmbeddingAdapter` is deterministic and dependency-light. It is kept
as a first-class `EmbeddingPort` implementation and documented as TF-IDF, not
marketed as a semantic model. Remote mode uses the existing
`OpenAICompatibleEmbeddingAdapter` and cannot silently substitute local vectors
when the provider fails.

## 5. One product pipeline

The CLI composition root reuses `MediaBranchOrchestrator`,
`VideoContextBuilder`, `VideoChunkingService`,
`VideoEvidenceRetrievalService`, `LongVideoContextService`, and
`AgentLoopService`. Presentation code handles arguments, progress, and
Markdown rendering; it does not fork business logic.

## 6. Operational boundaries are visible

Heavy local runtimes and live provider checks are kept out of the base install
and pytest. Credentials are process-only inputs. Tool paths are configurable,
timeouts are bounded, and the report distinguishes live verification from a
provider or configuration block.

## 7. Representative evidence

The accepted Phase 10B-FIX run used real FFmpeg and local `tiny.en` Whisper on
346.191474 seconds of media: 65 ASR spans, six temporal windows, two chunks,
multiple candidates, retrieval-selected 300–360 seconds, and one each of
Planner, Executor, and Critic with Evidence Guard PASS. The full baseline was
368 passed with no skipped tests. Phase 11 then verified the same two chunks
through the real SiliconFlow-compatible `BAAI/bge-m3` embedding endpoint:
four dimension-1024 vectors were returned and the existing retrieval still
selected the later After Love region without hardcoded retrieval.

## 8. A thin Web Demo without a second system

The Web Demo uses a standard-library HTTP adapter with generated upload IDs,
Range-capable video serving, a process-local presentation job registry, and
polling JSON status. Its runner constructs the same `VideoAnalysisApplication`
used by the CLI; the browser only uploads, renders reliable progress, maps the
existing result DTO, and seeks the native player to an evidence timestamp.
This keeps the demo easy to start without turning presentation state into a
competing durable task architecture.

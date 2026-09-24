# Interview notes

## What problem does the project solve?

It turns long videos into time-addressable evidence that an Agent can retrieve,
reason over, and present with a source range. The system is designed for
long-running, cost-sensitive work where a flat transcript and an unconstrained
LLM are not sufficient.

## Why is this more than RAG?

RAG is only the candidate-selection part. DOVideo preserves temporal
multimodal context, applies a long-context budget, executes a bounded
Planner–Executor–Critic loop, persists and recovers state at defined boundaries,
and programmatically checks that every final claim is supported by source text
and a covered timestamp.

## How does the pipeline preserve time?

ASR spans and OCR observations are merged into fixed 60-second `VideoSegment`
windows. `VideoChunkingService` groups them into five-minute chunks but keeps
the raw segments. Retrieval returns those raw ranges, so a final evidence
timestamp can be mapped back to a seekable source window.

## Why both local and remote embedding modes?

`EmbeddingPort` keeps the application independent of the provider. Local mode
is deterministic and offline; remote mode uses the existing OpenAI-compatible
adapter for a real semantic model. The choice is made in composition, so
retrieval scoring and the Evidence Guard do not change by provider.

## What is the difference between TF-IDF and a semantic embedding?

TF-IDF is a sparse lexical representation learned from the local corpus: terms
that occur often in a document but less often across documents receive more
weight. A pretrained semantic embedding is a dense vector learned from a
large corpus, where geometric distance can reflect related meaning even when
the exact words differ. `LocalTfidfEmbeddingAdapter` is therefore an honest
offline fallback, not a semantic-model substitute.

## How do Planner, Executor, and Critic differ?

Planner turns the goal into verifiable tasks. Executor produces a structured
draft with conclusions and evidence. Critic checks coverage, structure, and
evidence binding; a failed critique can cause a bounded retry/replan. The
AgentLoop owns round and budget limits.

## What happens when a provider fails?

Typed provider errors remain visible. The application has documented local
fallbacks for chunk summaries, embeddings, and vector lookup where the frozen
contract allows them. Remote embedding live verification is never reported as
successful when the provider or credential is unavailable.

## How was it tested?

Unit and integration tests inject HTTP/process/storage fakes; pytest never
contacts a live provider. Targeted regression, one final full pytest,
`compileall`, public-import smoke, and explicit real-media/Whisper runs cover
the boundaries. The canonical full baseline is 368 passed, zero failed, zero
skipped.

## Why keep heavy dependencies out of the base package?

Torch/Whisper, FFmpeg, Tesseract, and vector services are platform- and
operation-specific. Keeping them out of the contract/test install makes CI
fast and deterministic while the CLI documents exactly what a real local run
needs.

## What would you improve next?

I would add a managed durable job runner and Qdrant deployment around the
existing ports, then improve model observability and user-level cost controls.
Those are deployment/product extensions, not reasons to change the frozen
retrieval or evidence contracts in this phase.

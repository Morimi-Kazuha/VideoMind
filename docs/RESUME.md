# Resume-ready project summary

R0 scope note: describe the standard-library Web Demo as a temporary local
presentation aid only. The final product direction is Vue 3 + Vite, FastAPI,
and SSE; React is not part of the project.

## One-line version

Reconstructed a provider-neutral long-video Video Agent in Python, preserving
Java contracts while adding timestamped multimodal context, hybrid retrieval,
bounded Planner–Executor–Critic execution, and programmatic evidence
verification.

## 30-second introduction

I reconstructed a Java long-video analysis service in Python around stable
domain and application ports. A local video is converted into timestamped
Whisper/OCR observations, 60-second context windows, and five-minute chunks;
hybrid retrieval selects a time range, then the existing Planner–Executor–Critic
loop produces an evidence-bound answer. The developer CLI demonstrates a
deterministic local TF-IDF fallback and an explicit OpenAI-compatible remote
embedding adapter.

## 90-second introduction

The core engineering problem was preserving time and evidence across a long
video instead of flattening everything into one prompt. I kept the Java DTO
semantics and moved media, model, embedding, vector, and persistence I/O behind
injectable Python ports. The local path uses FFmpeg, segmented Whisper, and
Tesseract to build immutable 60-second windows; five-minute chunking and
hybrid vector/lexical retrieval retain source ranges. The existing bounded
AgentLoop then separates planning, execution, and critique, while the
Evidence Guard programmatically verifies claim text, source, and half-open
timestamp coverage.

The original Java baseline also includes Agent Evaluation metrics and an
AgentTelemetry-backed trace/latest-lookup surface. Those are original parity
responsibilities in R1–R4; the approved X2/X3 work extends them with deeper
provenance, structured replay, advanced metrics, and ablation rather than
creating parallel subsystems.

For validation, real 346.191474-second media produced 65 ASR spans, six
windows, two chunks, and a retrieval-selected 300–360-second later poem
region. Phase 11 also live-verified four `BAAI/bge-m3` vectors at dimension
1024 through the original OpenAI-compatible embedding boundary. I would be
careful to distinguish that verified path from unverified production Qdrant,
MySQL, Redis, queue, and deployment wiring.

## Resume bullets

- Designed immutable Pydantic domain contracts and application ports for
  timestamped ASR/OCR observations, five-minute chunks, retrieval hits,
  structured Agent output, budgets, and checkpoint state.
- Implemented a safe local media boundary with FFmpeg/ffprobe, segmented
  Whisper ASR, Tesseract OCR, scoped workspaces, retry/error mapping, and
  branch-level degradation.
- Built hybrid temporal retrieval combining vector, lexical, and OCR signals;
  preserved source windows so evidence can be sought directly in the video.
- Implemented a bounded Planner–Executor–Critic loop with checkpoint recovery,
  deadline/token/cost guards, policy validation, and an authoritative evidence
  gate for timestamp/source/claim binding.
- Added a single product composition root and `python -m dovideo analyze`
  command with local TF-IDF default, explicit OpenAI-compatible remote
  embedding mode, progress reporting, sanitized Markdown output, and offline
  deterministic tests.
- Added a thin Python-standard-library Web Demo with safe staged uploads,
  polling progress, structured result/evidence rendering, native video Range
  playback, and timestamp-click seeking through the same composition root.

## Measured validation

The representative Phase 10B-FIX media was 346.191474 seconds. Real FFmpeg,
real local `tiny.en` Whisper, 65 timestamped ASR spans, six context windows,
two chunks, multiple retrieval candidates, and a retrieval-selected 300–360
second later semantic region were verified. Planner, Executor, and Critic each
ran once and Evidence Guard passed. The final full suite was 368 passed,
zero failed, zero skipped.

## Honest limitations

The Python CLI currently uses a process-local vector index for one analysis;
production Qdrant and durable service wiring are separate infrastructure work.
Local TF-IDF is an explicit fallback and not a pretrained semantic embedding.

# DOVideo Python

DOVideo Python is the Python-native reconstruction of the public
DOVideo-AI project. It turns video into timestamped ASR/OCR observations,
immutable temporal VideoContext windows, five-minute chunks, hybrid evidence
retrieval, and a bounded Planner–Executor–Critic AgentLoop. The final result
is accepted only when the Evidence Guard binds claims to source text and a
covered time range.

## Scope status

R0 — Scope Realignment & Architecture Consolidation is accepted and frozen.
R1 — FastAPI + Vue + SSE Product Parity is implemented with a bounded local
development composition. The final product direction is:

R2 — SQLAlchemy/MySQL/Redis/MinIO/Qdrant is implemented and live-verified.
Docker Desktop, its WSL data root, service persistence, and generated local
development credentials remain on D:. See LUNA_REPORT.md and HANDOFF.md for
the evidence and current boundary.

~~~text
Vue 3 + Vite → FastAPI → Celery + RabbitMQ → existing application/task
semantics → MySQL + Redis + MinIO + Qdrant → FastAPI SSE → Vue
~~~

R1–R4 implement original product parity. X1–X3 are the only approved Python
enhancements. React, Next.js, another frontend, Event Sourcing, unrestricted
ReAct, a second queue/database/vector store/object store, and generic
observability platforms are outside scope. See docs/SCOPE_LOCK.md and
docs/PARITY_MATRIX.md.

The Vue client and FastAPI app are the canonical R1 product path. The CLI
remains a developer/debug path. The standard-library Web Demo is retained as
DEPRECATED/TEMPORARY_DEMO only; no React path is supported.

R2 production profile

Set `DOVIDEO_PROFILE=production` and provide the local R2 environment values
from `.env.r2.local` (the file is ignored and contains no user API keys).
Start the four local infrastructure services with `docker-compose.r2.yml`,
then run the FastAPI app using the same environment. The production profile
uses SQLAlchemy/MySQL for durable records, Redis for hot state and locks,
MinIO for media/chunks, and Qdrant for vectors. It fails fast when required
production settings are absent; it does not silently fall back to local
storage.

## Quick start

Use Python 3.12 or newer and install the package from the repository root:

~~~bash
python -m pip install -e ".[test]"
~~~

The developer/debug command is:

~~~bash
python -m dovideo analyze VIDEO_PATH --goal "What is the main argument?"
~~~

It prints progress to stderr and a provider-neutral Markdown result to stdout.
Use --output result.md to save the result. Local TF-IDF vectors are the
offline default; --embedding-mode remote opts into the existing
OpenAI-compatible semantic adapter.

The command requires a configured OpenAI-compatible structured-model endpoint
for Planner, Executor, and Critic. Load credentials into the process
environment only; the CLI never prints or writes them. The local Whisper
runtime is intentionally kept outside the base package because it carries
Torch.

For the provisioned Windows runtime:

~~~powershell
$env:PYTHONPATH = "src;tools/asr/python-packages"
& D:\python\python.exe -m dovideo analyze .\work\media\representative-long.mp4 --goal "What does the later poem say about the sea, pool, and tide?" --embedding-mode local
~~~

## Legacy Web Demo

The old Web Demo is retained temporarily as a local presentation aid. Start
it with:

~~~powershell
$env:PYTHONPATH = "src;tools/asr/python-packages"
& D:\python\python.exe -m dovideo web
~~~

Open http://127.0.0.1:8765. It demonstrates safe local upload, progress
polling, guarded result rendering, native playback, and timestamp seeking.
It uses the same VideoAnalysisApplication as the CLI, has no browser-side
provider calls, and exposes no API credentials.

This server is explicitly TEMPORARY_DEMO: process-local jobs and uploads,
standard-library HTTP, no authentication, no durable task history, and no
distributed worker. It must not be presented as a production SaaS path. It
can be removed after the Vue/FastAPI path is validated in R1.

## R1 FastAPI + Vue path

Start the bounded local API from the environment where the Quick start
dependencies were installed:

~~~powershell
python -m dovideo api
~~~

The R1 API preserves the original `/user`, `/media`, and `/analysis` route
names, the `{code,message,data}` envelope, Bearer authentication, the 5 MiB
resumable-upload protocol, and FastAPI `text/event-stream` frames. Its local
filesystem/in-memory stores are explicitly DEV/TEST ONLY; SQLAlchemy/MySQL,
Redis, MinIO, and Qdrant production composition remain R2 work.

The canonical Vue client is in `client/`:

~~~powershell
cd client
npm ci
npm run dev
~~~

## Reliable Video Task Chain

The original product separates large uploads and expensive analysis from the
request thread. The final Python path will use FastAPI submission, original
idempotency/task semantics, Celery + RabbitMQ execution, and MySQL/Redis
state. The current application already provides transport-neutral dispatch,
task lifecycle, retry classification, checkpoint recovery, and durable
dead-letter handoff semantics for those future adapters.

## Temporal Multimodal VideoContext

FFmpeg creates the existing audio and frame observations. ASR and OCR run
through separate Python adapters, then VideoContextBuilder merges text,
frames, and timestamps into stable temporal windows. A failed branch can
degrade without discarding a healthy branch, while both-branch failure is
preserved as an error.

## Evidence-Constrained AgentLoop

The single explicit AgentLoop resolves a bounded plan, repairs invalid plans,
runs Executor rounds, invokes Critic, and performs targeted retrieval when
Critic feedback identifies an evidence gap. EvidenceVerificationService is
the authoritative gate: prompt instructions and retrieval scores alone never
make a claim valid.

## Long-Video Retrieval & Recovery

VideoChunkingService uses five-minute temporal buckets. Hybrid retrieval
combines semantic, keyword, and visual signals, and LongVideoContextService
applies the bounded context budget. Checkpoint and worker recovery preserve
the accepted plan/Critic/result semantics. Qdrant, MySQL, Redis, and MinIO
are the future production composition; their current adapters are not
silently promoted by the local CLI.

## Python-native Enhancements

The original parity baseline already includes timestamp/source/claim evidence
grounding. The original Java project also exposes Agent Evaluation and
AgentTelemetry/Trace surfaces; those baselines belong to R1–R4 and are not
Python-only inventions.

Only these four delta families are approved:

- Deep Evidence Provenance: result → evidence → ASR/OCR → VideoContext →
  timestamp/window → original video.
- Restricted Tool Calling: search_video, get_segment, get_transcript, and
  find_visual_evidence through a validated allow-list.
- Structured Execution Trace / Replay: one structured extension of the
  original AgentTelemetry trace_id for a single Agent execution chain.
- Advanced Evaluation / Ablation: extend the original evaluation metrics with
  Recall@K, Evidence Hit Rate, Groundedness, Critic Pass Rate,
  round/token/cost/latency, and bounded ablations.

The current repository has partial provenance fields, telemetry hooks, and
Evidence Guard facts, but no complete original evaluation/trace product
surfaces and no full X1–X3 implementation. Existing baseline telemetry,
tests, and checkpoint recovery semantics are not relabeled as the enhanced
Trace or Evaluation families.

## Provider and fallback roles

| Component | Current role |
| --- | --- |
| OpenAI-compatible BGE-M3 adapter | production semantic embedding path |
| Qdrant adapter | future production vector path; offline-tested |
| SQLite and in-memory stores | test/local fallbacks |
| Local TF-IDF | offline/test/fallback embedding |
| DB-API MySQL and Redis adapters | production-boundary adapters; R2 wiring remains |
| CLI | developer/debug utility |
| standard-library Web Demo | legacy temporary demo |

Remote embedding sends POST to the configured base URL plus /embeddings with
the configured model and input, and decodes data[0].embedding. The prior
representative live run used BGE-M3 through an OpenAI-compatible endpoint;
credentials were process-only and are not part of the repository.

## Verified reconstruction baseline

The previously accepted real-media E2E work, completed before R0-FIX, used the
prepared 346.191474-second representative media with real FFmpeg and local
tiny.en Whisper. It produced 65 ASR spans, six VideoContext windows, two five-minute chunks, multiple
retrieval candidates, and selected the later 300000–360000 ms After Love
region without hardcoded transcript or retrieval. The existing AgentLoop ran
real Planner, Executor, and Critic roles and Evidence Guard passed.

The latest known Python regression is 383 passed / 0 failed / 0 skipped.
R0 does not rerun expensive Whisper or LLM inference.

## Runtime requirements

- FFmpeg and ffprobe for media probing, audio segmentation, and keyframes.
- Local OpenAI Whisper with its Torch runtime for the local ASR path.
- Tesseract for OCR.
- An OpenAI-compatible structured-model endpoint for a real AgentLoop run.
- A remote embedding credential only when remote BGE-M3 mode is selected.

eSpeak/eSpeak NG is not required and is not part of the formal ASR path.
The repository does not reinstall or repair it.

## Checks

~~~bash
python -m pytest
python -m compileall -q src tests
~~~

The automated suite is offline and uses injected process/HTTP/provider fakes.
Live media/provider checks are explicit operator-run validation, not pytest
requirements.

## Documentation

- docs/SCOPE_LOCK.md — locked purpose, scope, mapping, and canonical roles.
- docs/PARITY_MATRIX.md — original capability audit and current statuses.
- docs/ARCHITECTURE.md — current core and final production direction.
- docs/REFACTOR_PLAN.md — the only R0–R4/X1–X3 roadmap.
- docs/INTERVIEW_SCOPE.md — bounded study boundary.
- docs/MIGRATION_MATRIX.md — detailed historical Java-to-Python contracts.
- LUNA_REPORT.md — current R2 execution report and Web Sol handoff.

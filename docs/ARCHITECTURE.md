# DOVideo Python architecture

R0 fixes the architecture story without changing the accepted Python core.
The target is an original-parity product backend, not a second demo platform.

R2 SQLAlchemy/MySQL/Redis/MinIO/Qdrant composition is implemented and live
verified with Docker Desktop and its Docker/WSL data on D:. R0/R1 remain
accepted and frozen. The bounded evidence is recorded in `LUNA_REPORT.md` and
`HANDOFF.md`.

## Canonical production direction

The final product path is:

~~~text
Vue 3 + Vite
        │ REST + SSE
        ▼
FastAPI + Pydantic
        │
        ├── API/read path
        └── Celery + RabbitMQ
                │
                ▼
        Existing TaskWorker/lifecycle semantics
                │
                ▼
        FFmpeg → ASR/OCR → VideoContext
                │
                ▼
        five-minute chunks → hybrid retrieval → Qdrant
                │
                ▼
        explicit Agent Runtime
        Planner → Executor → Critic → Evidence Guard
                │
                ├── MySQL durable truth
                ├── Redis hot state/lock/idempotency
                ├── MinIO media/evidence objects
                └── original AgentTelemetry/AgentEvaluation baselines
                    └── X2/X3 structured extensions
~~~

R1–R4 implement the Category A path in bounded steps, including the original
timestamp-evidence behavior and Agent Evaluation/AgentTelemetry trace
surfaces. X1–X3 implement only the four approved Python deltas.

## Current accepted Python core

The application and domain layers remain provider-neutral. The accepted flow
is:

~~~text
media observations
  → VideoContextBuilder
  → VideoChunkingService
  → VideoEvidenceRetrievalService
  → LongVideoContextService
  → AgentLoopService
  → EvidenceVerificationService
~~~

The core already contains timestamped ASR/OCR context construction,
five-minute chunking, lexical/cosine hybrid retrieval, context budgeting,
Planner–Executor–Critic orchestration, evidence-bound validation, checkpoint
recovery, task lifecycle, retry classification, and durable dead-letter
handoff semantics. These are preserved as the single application behavior.

The original Java baseline also defines timestamp evidence grounding,
AgentEvaluationService metrics, and AgentTelemetry/agent-trace behavior. The
Python reconstruction must add those original product surfaces in the
Category A roadmap; X2 and X3 extend them rather than creating parallel
subsystems.

The real local representative validation previously proved the existing
FFmpeg → local tiny.en Whisper → VideoContext → multi-window chunking →
retrieval → real Planner/Executor/Critic → Evidence Guard path. R0 does not
rerun expensive media or model inference.

## Current developer composition

The existing composition root is a local developer/debug path:

~~~text
python -m dovideo analyze
        │
        ▼
VideoAnalysisApplication
        │
        ├── local FFmpeg/Whisper/Tesseract
        ├── Local TF-IDF or OpenAI-compatible BGE-M3
        ├── process-local vector index
        └── existing application AgentLoop
~~~

The OpenAI-compatible BGE-M3 adapter is the canonical semantic embedding
seam. The local TF-IDF adapter and process-local vector index are explicitly
fallback/test/local roles. R2 composes SQLAlchemy 2.x/MySQL, Redis, MinIO,
and Qdrant behind the existing ports; DB-API adapters remain internal
compatibility seams, not a second production composition.

## Presentation boundaries

The CLI remains a developer/debug utility. The standard-library Web Demo
under src/dovideo/web.py is a legacy temporary presentation used to prove
upload, progress, result rendering, and timestamp seeking. It delegates to
the same application composition root, owns no second AgentLoop or retrieval
algorithm, and is not the final product frontend.

The final product frontend is Vue 3 + Vite. No React or Next.js path is
supported, and no React files or dependencies were found during the R0 audit.
The legacy Web Demo can be removed after the Vue/FastAPI path is validated in
R1.

## Persistence and transport roles

| Responsibility | Current local/test role | Final production role |
| --- | --- | --- |
| durable checkpoint/media records | SQLite, in-memory, DB-API seams | SQLAlchemy 2.x + MySQL |
| hot checkpoint/task state | in-memory and Redis-compatible adapter | Redis |
| media/evidence objects | local/in-memory stores | MinIO |
| vector index | process-local index | Qdrant |
| task transport | provider-neutral dispatch/worker ports | Celery + RabbitMQ |
| progress delivery | task events/status projection and Web polling | FastAPI SSE |

There is one production implementation per responsibility. Local adapters are
not a competing production path.

## Approved enhancement boundaries

The original timestamp-evidence, evaluation, and telemetry/trace behavior is
parity scope. The only future Python extensions are:

- Restricted Tool Calling for four allow-listed video tools;
- Deep Evidence Provenance from result to source and original video;
- Structured Execution Trace / Replay extending one AgentTelemetry trace;
- Advanced Evaluation / Ablation extending one AgentEvaluation metric path.

No Event Sourcing, unrestricted ReAct, arbitrary Agent framework, second
message queue, second frontend, duplicate trace system, duplicate evaluation
system, or generic observability platform belongs here.

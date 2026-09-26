# DOVideo

DOVideo is a Python-native long-video understanding system. It extracts timed
speech and frame text, retrieves relevant video evidence, and runs a bounded
Video Agent that returns structured answers with source ranges. This repository
adapts the public [DOVideo-AI](https://github.com/Xiaoc7r/DOVideo-AI)
project into a FastAPI, Celery, and Vue application.

## Why DOVideo

A long video rarely fits a useful single-context prompt. Relevant facts may
appear minutes apart in speech or on-screen text. DOVideo preserves those
sources and their timestamps through retrieval and answer generation so that
an Agent response can be checked against the video rather than accepted on
model confidence alone.

## Architecture

```mermaid
flowchart LR
    Video --> Extract[FFmpeg + ASR / OCR]
    Extract --> Context[Temporal VideoContext]
    Context --> Chunks[Long-context chunks]
    Chunks --> Retrieval[Hybrid retrieval]
    Retrieval --> Agent[Bounded AgentLoop]
    Agent --> Planner --> Executor --> Critic --> Guard[Evidence Guard]
    Guard --> Result[Structured result + timestamps]
```

Vue 3 and Vite provide the client; FastAPI exposes REST and SSE. Celery and
RabbitMQ move long-running work off the request path. MySQL stores durable
application state, Redis supports operational state and limits, MinIO stores
media, and Qdrant indexes vectors. The CLI and local adapters provide smaller
developer paths through the same application logic. See
[architecture](docs/ARCHITECTURE.md) for the request lifecycle and authority
boundaries.

## Core engineering

- **Multimodal context.** FFmpeg supplies audio segments and keyframes;
  Whisper ASR and Tesseract OCR become timestamped source observations.
  `VideoContextBuilder` combines them into temporal windows.
- **Long-video retrieval.** Five-minute chunks carry summaries and source
  ranges. Hybrid semantic and lexical retrieval proposes evidence while
  preserving the original timed spans.
- **Bounded AgentLoop.** Planner creates verifiable tasks, Executor produces a
  structured draft, and Critic checks the draft within explicit round and
  token budgets. Evidence Guard checks final claims against source text and
  covered time ranges.
- **Controlled tools.** A model can request only the registered, same-video,
  read-only evidence tools. `AgentLoop` and `ToolPolicy` authorize and execute
  each request; a model response never grants itself tool authority.
- **Operational controls.** Task idempotency, bounded retries, checkpoints,
  failed-task handling, and AI interaction rate limits constrain long and
  costly work.

## Provenance and replay

A durable execution record is the historical source of truth. A checkpoint
supports crash recovery; Redis is an operational projection. Historical
replay reads the durable record without calling a model, retrieval service,
tool, or Jev again.

During evaluation, temporary OCR file paths were found in a source-identity
calculation. Moving the same media between workspaces could change provenance
IDs. Stable frame identities fixed that defect; a later Whisper span change
improved timed evidence granularity. Dataset v1 remains historical, and
`golden-dataset-v2` binds its annotations to the corrected source revision.
See [OCR provenance](docs/PROVENANCE_V2.md) and
[ASR granularity](docs/ASR_GRANULARITY_V3.md).

## Adaptive routing and evaluation

The formal X3 experiment used three heterogeneous execution lanes:

| Lane | Frozen model behavior |
| --- | --- |
| FAST | DeepSeek V4.1 Flash, reasoning `none` |
| BALANCED | DeepSeek V4.1 Flash, reasoning omitted/provider default |
| DEEP | DeepSeek V4 Pro, reasoning `max`, 65,536 max tokens |

Model execution used OpenRouter with a fixed `nextbit/fp8` provider pin and
fallback disabled. Jev (`typesafe/jev-1.13`) advised routing through a
separate OpenRouter path; DOVideo's deterministic policy retained final
authority. The frozen configuration is identified by commit `d0c8480` and
the `golden-dataset-v2` logical digest
`7f374396c5eb002ba158717afaffa8a65f5ee64ed79663f1f6b6047960dae8e4`.

The full X3-C campaign persisted **80/80** planned case-strategy results.
**Zero** passed its preregistered quality gate. Both adaptive router arms
ultimately chose BALANCED for all 16 cases, so this benchmark did **not**
establish an advantage for adaptive routing. It is a bounded negative result.
Provider-reported cost totaled **USD 1.170390452 for 73/80 cases with complete
case telemetry**; that is a partial subtotal, not the full campaign charge.
The [final X3 report](docs/X3_OPENROUTER_X3_E_REPORT.md) gives the outcomes,
failure categories, measurement limits, and separate derived ablations.

### What the evaluation taught us

Working routing code is distinct from evidence of better outcomes. Freezing
the gate and router before model results prevented threshold changes made to
improve a score. Provider and regional availability affected the experiment
design, and provenance correctness had to be established before evidence
metrics were trustworthy. Negative results and earlier interrupted campaigns
were preserved rather than merged into the final comparison.

## Quick start

Use Python 3.12 or newer. From the repository root:

```bash
python -m venv .venv
# Activate .venv for your shell; on Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pytest -q
python -m dovideo api
```

The API defaults to `127.0.0.1:8000`; `--host` and `--port` are available.
The Vue client starts separately:

```bash
cd client
npm ci
npm run dev
```

For a real video analysis, install FFmpeg/ffprobe, Tesseract, and a supported
Whisper runtime, then configure a structured chat provider in the process
environment. The sanitized [.env.example](.env.example) lists local service
and provider variable names. The CLI developer path is:

```bash
python -m dovideo analyze /path/to/video.mp4 --goal "Summarize the evidence"
```

The optional service composition uses MySQL, Redis, MinIO, Qdrant, and
RabbitMQ. Copy `.env.example` to an ignored `.env.r2.local`, replace every
placeholder, and choose a writable `DOVIDEO_R2_DATA_ROOT` before running:

```bash
docker compose --env-file .env.r2.local -f docker-compose.r2.yml up -d
```

Compose's `--env-file` configures the containers; load the needed variables
into the backend process separately. Set `DOVIDEO_PROFILE=production` only
when using the R2 production composition. The historical X3 campaign is not
a quick-start task and requires prepared media and external services.

## Testing and project status

The final local verification passed **839 Python tests**, the client tests,
client build, Python compilation, and public import checks. The Python suite
uses fakes for external model and infrastructure calls; live-provider checks
are separate. Run the client checks with `npm test` and `npm run build` from
`client/`.

The engineering implementation and formal X3 evaluation are complete. The
repository documents a local/developer system and bounded live validation;
it is not presented as a deployed production SaaS. The dataset covers one
media source and 16 cases, costs are partly observed, and external provider
behavior can change.

## Repository layout

```text
src/dovideo/       domain, application, infrastructure, API, and CLI
client/            Vue 3/Vite frontend
tests/             offline Python tests
datasets/x3/       versioned evaluation annotations
configs/x3c/       frozen routing and gate configurations
docs/              architecture, provenance, and evaluation evidence
tools/             explicit developer and evaluation utilities
```

The main stack is Python 3.12+, FastAPI, Pydantic, SQLAlchemy, Celery,
RabbitMQ, MySQL, Redis, MinIO, Qdrant, Vue 3, FFmpeg, Whisper, and Tesseract.

## License

MIT. The adapted frontend retains the
[upstream copyright and license notice](client/NOTICE.md) and the root
[LICENSE](LICENSE).

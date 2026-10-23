# Backend engineering: migrations, content preparation, deployment validation

## Schema ownership and deployment

SQLAlchemy models describe current tables. Alembic revisions are frozen schema
snapshots and own upgrades. Production `R2Infrastructure.initialize()` only
checks the database revision; API and Celery workers never execute migrations.
The compatibility `create_schema(engine)` helper now explicitly invokes Alembic
for tests/scripts, rather than `metadata.create_all()` plus handwritten DDL.

Use Python 3.12 for deployment and primary validation (3.13 has a compatibility
CI job). From a source checkout, install
`python -m pip install -c constraints-py312.txt -e ".[test]"`, export the
private infrastructure configuration, back up an existing database, stop older
writers during schema upgrades, and run one deployment job:

```bash
alembic upgrade head
alembic current
alembic check
python -m dovideo api
# Start Celery workers only after migration succeeds.
```

On Linux an operator can bound the whole migration job with
`timeout 180s alembic upgrade head`; choose a release timeout appropriate to table
size. MySQL connection/read/write timeouts are bounded, and Alembic uses a
connection-owned MySQL advisory lock with a 30-second acquisition limit plus
`lock_wait_timeout=30`. The same lock covers the explicit helper path. Errors
propagate. MySQL DDL is not transactional: inspect failed upgrades and restore a
verified backup when necessary. Do not blindly stamp an existing database.

The online baseline adopts unversioned VideoMind tables and creates missing
tables, optional columns, safely backfilled default columns, indexes, unique
constraints and foreign keys. It preserves existing data. Missing historical
identities on populated tables and conflicting keys/indexes fail closed instead
of inventing execution/provenance history. Unknown historical database variants
need an explicit reviewed migration; this is not an arbitrary schema repair tool.
The second revision widens MySQL checkpoint TEXT/MEDIUMTEXT to LONGTEXT. SQLite
uses TEXT and batch column/constraint changes for ephemeral tests. The third
revision creates the independent content artifact table. The fourth adds a
nullable `media_files.content_identity` proof; no historical binding is invented.
Baseline adoption and
LONGTEXT widening refuse potentially destructive downgrades.

Production metadata consists of `users`, `media_files`, `agent_checkpoints`,
`failed_analysis_tasks`, `failed_analysis_task_replays`,
`agent_execution_records`, `agent_execution_events` and now
`content_context_artifacts`. Task lifecycle, pending DLQ handoff, model routing,
tool ledger, follow-up recovery and context/chunk recovery use typed checkpoint
rows. Existing SQLite/raw-DB adapters remain local/testing boundaries, outside
the R2/R4 production bootstrap.

## Content identities and production call chain

Uploads stream SHA-256 during ordered chunk merge and persist the full hex
digest in `media_files.content_hash`; direct-file and URL ingestion use the same
algorithm. The existing nullable `VARCHAR(64)` stores both new 64-character
SHA-256 and historical 32-character MD5 values without a schema migration.
Repositories preserve historical records on read. A legacy MD5 is never treated
as SHA-256, padded or converted: any recomputation requires real video bytes.
There is no bulk rewrite, media deletion or forced rebuild of valid artifacts.
Legacy task/failed-replay normalization retains its MD5-or-media-ID policy;
TaskKey, uploadId, requestId and Checkpoint responsibilities are unchanged.

Neither an untrusted transport hash nor a digest alone authorizes a reuse hit.
Authenticated API admission checks media ownership and places the authenticated
`userId` on the internal JSON message. R4 reloads the MySQL media record, checks
that owner against the principal, and uses the record's source and digest instead
of message metadata. The broker remains a trusted internal service, not a public
request endpoint. Messages from older producers without `userId` retain the cold
path and cannot establish a fast-path binding; TaskKey and redelivery stay intact.

`content_identity` stores a schema-versioned source + SHA-256 + MinIO version ID
binding. It is written conditionally only after downloading that exact pinned
non-null version and hashing the real bytes against the trusted SHA-256. On later
requests a current-object HEAD must match the binding before artifact lookup;
after lookup the record, ownership and current version are checked again.
Version IDs prove which immutable byte version was verified; ETags are not used
as SHA-256. Replacement/deletion cannot silently authorize an old artifact.
SHA-256 mismatch fails before preprocessing or publication. Missing/corrupt
bindings, missing artifacts, legacy/invalid hashes and unversioned objects use
download + verification + the existing content build lock. Lookup outages are
misses; Redis hints still cannot authorize a hit without the durable artifact.

Each separately uploaded original object needs its own initial verified binding,
even if another user's same-byte artifact exists. That first download may reuse
ASR/OCR; later valid hits for the bound object use zero `get_object` calls and
zero ASR/OCR calls in context preparation. Content locks retain their existing
scope and lease/wait policy; concurrent misses may download separately but only
one valid preprocessing build is published. No new lock or cache table exists.

Deploy with `alembic upgrade head` before restarting API/workers.
Upgrade workers before API producers: old workers' strict message parsers do not
understand `userId`; new workers can consume old messages through the cold path.
For fast reuse, operators must enable MinIO bucket versioning and retain readable versions;
the application does not silently enable versioning or change lifecycle/physical
deletion policy. Null versions (including older objects in a newly versioned
bucket) safely keep downloading. Versioned storage retention and deletion need
an operator policy; this feature does not migrate or deduplicate raw objects.

视频上传时计算并保存 SHA-256 内容指纹。分析阶段，在验证媒体身份和权限后，通过 SHA-256 + Pipeline Contract 查找已保存的 VideoContext。符合快速复用条件时，可跳过重复视频下载和 ASR/OCR；未命中或无法安全验证时，回到下载、核验和构建流程。
分析任务仍使用独立的 TaskKey 做幂等协调。
There is no pre-upload hashing or instant-upload feature. Content reuse covers
VideoContext preprocessing, not global reuse of every Chunk, Embedding or Agent
final result. SourceRevision and provenance validation remain mandatory.

`ContentContextKey` combines the SHA-256 fingerprint and pipeline contract. The
contract includes `DOVIDEO_CONTEXT_PIPELINE_VERSION`, Whisper model/device/
language, extraction/normalization/provenance contracts, audio segment size,
keyframe selection defaults, OCR deduplication threshold and context window
policy. Bump `DOVIDEO_CONTEXT_PIPELINE_VERSION` when tool binaries, model weights,
Tesseract environment/language data or any preprocessing semantics change.
Filesystem paths and secrets are excluded. SHA-256 of the structured key gives
the artifact identity and the separate content-lock namespace.

```text
Celery registered task
  -> R4WorkerRuntime.process (bind request)
  -> TaskWorker.handle (existing renewable Analysis TaskLease)
  -> R4RequestContextCheckpoint.load_context
  -> R4MediaPipeline.build_context
  -> reload owned MySQL media -> inspect current MinIO version
     -> matching verified binding -> validated ContentContextKey artifact hit
        -> recheck owner/version -> rebind source/goal (no video download)
     -> otherwise target MinIO download (pin version when available)
        -> streaming SHA-256 -> verify expected digest -> bind verified version
        -> versioned ContentContextKey
  -> ContentContextPreparation
     -> confirmed artifact lookup
     -> RedisContentBuildLock acquire -> double-check -> build once
     -> R4MediaPipeline._build_local
        -> ffprobe -> MediaBranchOrchestrator (FFmpeg / Whisper / Tesseract)
        -> VideoContextBuilder using content+pipeline identity
     -> durable content artifact publication
     -> bind target source and request goal
  -> save target media context checkpoint (invalidate old-revision chunks)
  -> TaskWorker -> existing AgentLoop / routing / execution record
```

The application service imports no Redis, SQLAlchemy, Celery, MinIO or provider
SDK. `ContentArtifactPort` and `ContentBuildLockPort` are provider-neutral.
Infrastructure implements persistence and Redis coordination. AgentLoop,
Planner/Executor/Critic prompts, tool contracts, evidence semantics, model
routing, uploads and TaskLease are unchanged.

## Persistence, authorization and provenance

`content_context_artifacts` stores immutable content-derived VideoContext JSON,
fingerprint, pipeline contract, payload digest and creation time. It has no
media/user/task foreign key. Its source is a content URI and its goal is empty;
it contains no owner media ID, object URL, path, execution record or private frame
location. Only stable `frame_<sha256>` references are accepted. The adapter
reconstructs the context from original ASR/OCR observations and checks exact
provenance equivalence before publication and on every hit.

Redis can cache the same neutral payload for seven days. A hit still confirms
the independent MySQL artifact header and digest; deferring LONGTEXT transfer
reduces database I/O. Missing durable artifacts clear orphan Redis entries.
Corrupt payloads/key mismatches/private references are discarded and rebuilt.
Redis absence/outage uses the durable payload. Source media deletion or its
checkpoint cleanup cannot remove the independent artifact. Durable artifacts
currently have no automated retention policy; future content-based GC must
preserve this independence and any deletion-policy requirements.

The returned context always receives the requested target source and goal, then
is persisted under that target media ID. Existing API `require_owned` gates
media submission and read surfaces; cache objects are not publicly addressable.
Cross-user reuse therefore shares derived source observations while preserving
the target presentation/authorization boundary. Source revision is calculated
from content+pipeline identity and observations. Segment IDs, source item IDs,
content digests and stable frame identities remain unchanged by rebinding.
Citations validate against the target context; playback APIs and frontend jumps
continue using the target media. Historical replay uses each task's independent
execution record and existing owner checks.

An old media context cannot bypass pipeline invalidation: a bound worker request
prepares against current target bytes and current contract. If source revision
changes, the old per-media chunks are cleared before context save. Chunk
summaries/embeddings are never shared across media in this change.

## Coordination and failure policy

The content lease is separate from the analysis task lease. Its default TTL is
120 seconds and renewal occurs every TTL/3, because preprocessing can take up to
an hour. `RedisContentBuildLock` reuses the existing SET NX PX, random owner token,
compare-token renewal/release implementation; there is no second Lua policy.
Double-check lookup after acquisition avoids rebuilding another owner's output.

Initial cache/coordination outage permits a private current-media build and
skips shared publication when no content lease was obtained. Durable artifact
publication failure also permits current task progress. Once live contention
has been observed, outage or bounded wait expiry raises transport recovery
instead of starting another expensive concurrent build. Loss of an acquired
content lease cancels cooperative preprocessing and prevents publication. Task
lease guards still run at durable target checkpoints.

This reuse-specific optimization is not a correctness dependency. The existing
analysis TaskLease, durable task checkpoint/Execution Record, broker and storage
remain their own correctness/recovery dependencies. A global Redis outage can
still affect the original task lease; this change does not claim to make the
whole application independent of Redis. There is no exactly-once guarantee;
TTL expiry, crashes or initial cache outages can cause safe rebuilding. Partial
ASR/OCR branch failure stays private and is not published for shared reuse.

## Validation and reproduction

```bash
python -m pytest -q
python scripts/validate_deployment.py --env-file .env.example
docker compose --env-file .env.example -f docker-compose.r2.yml config --quiet
python -m compileall -q src scripts alembic
git diff --check
cd client && npm test && npm run build
```

CI performs these offline tests/composition checks, Compose syntax/interpolation
validation, and SQLite `upgrade head` twice, `current` and `alembic check`.
The sanitized env template uses explicit fake endpoints and `replace-me`
credentials. The smoke checks known configuration names, placeholders, required
settings, URL/provider shapes and actual R4 API/worker constructors with network
access forbidden. It neither starts paid providers nor sends model requests.

Real MySQL/Redis/MinIO proof reuses local infrastructure and admin credentials
from an ignored file; it creates UUID-named databases and objects and removes
only those generated fixtures:

```bash
python scripts/run_engineering_live_tests.py --env-file /path/to/private-infrastructure.env --include-upload
```

The MySQL tests check no model drift, legacy data preservation and a 100 KB
checkpoint payload after widening. The R4 media download/hash test uses generated
bytes with deterministic preprocessing observations; it does not claim paid ASR
or a real-video provider run. Content concurrency tests use real Redis tokens,
renewal and MySQL persistence. Existing task-lease live tests prove stale-token
safety and long-work exclusion.

Optional Linux prefork child-loss validation is **NOT RUN on the Windows host**.
The script refuses non-Linux execution rather than reporting a pass:

```bash
# Use a disposable, already migrated MySQL DB with RabbitMQ/Redis/MinIO configured.
python scripts/run_linux_prefork_probe.py --env-file /path/to/private-infrastructure.env
```

It selects the explicit deterministic R3 test runtime, SIGKILLs its single
prefork child, restarts the worker, checks saved-result recovery, injects one DLQ
publication outage, and reuses the existing Redis stale-token tests. Lock
contention itself keeps attempt 1. Actual re-execution after child loss follows
the existing lifecycle semantics (two Agent invocations/two business attempts),
not a fabricated exactly-once claim. This optional script is source-reviewed and
compiled, but its Linux behavior still requires execution in a Linux environment.

No final Agent result reuse, no Execution Record sharing, no cross-user source
exposure, no RocketMQ/Redisson and no AgentLoop redesign. Future final-result
caching needs a separate complete contract covering retrieval, model routing,
tool policy, execution identity and provenance.

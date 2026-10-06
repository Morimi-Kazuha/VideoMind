# VideoMind Upload Reliability Finalization

Date: 2026-10-06. Repository: https://github.com/Morimi-Kazuha/VideoMind

## 1. Status

PASS WITH NOTES. Upload regressions, existing backend suite, frontend suite,
production build and real Redis/MinIO/MySQL integration verification pass.
Notes: finite coordination/lease windows, best-effort orphan cleanup and the
existing Starlette/httpx deprecation warning remain explicit limits. This run
does not claim a maximum-size 2 GiB throughput or Redis failover soak test.

## 2. Baseline

- Intended branch: `main`.
- HEAD before: `30504e52352a7eb94bb98efc9db17d865f21c760`, confirmed against remote main.
- Original checkout: `D:\Agent Learning\dovideo-python`, with unrelated modified
  and untracked files. It was not edited, reset, rebased or cleaned.
- Work completed in a clean independent main clone:
  `D:\Agent Learning\VideoMind-upload-finalization`.
- Baseline upload infrastructure + API: 17 passed.

## 3. Current-source audit

Already implemented: UUID UploadSession, 24-hour expiry, deterministic temporary
part keys, ownership, bounded chunk limits, ordered local merge, incremental MD5,
completion receipts, best-effort cleanup and actual Redis/MinIO/SQL production
wiring. Browser already used 5 MiB chunks, concurrency 3, 410-chunk maximum,
four transient-only attempts with exponential jitter, user-scoped credentials,
server status, cancellation and lost-completion-response recovery.

The [CURRENT / TARGET / GAP / ACTION matrix](UPLOAD_FINALIZATION_AUDIT.md)
was written before implementation. Ports remain useful for adapter isolation
and deterministic failure/concurrency tests.

## 4. Gaps found

1. MinIO listing was progress truth; no Redis chunk Set.
2. Unconditional Redis renewal could resurrect completed metadata. Renewal
   failure deleted the chunk, including a previously valid duplicate.
3. Merge lock was custom SET NX PX/Lua rather than redis-py Lock, ran on the
   event loop, and had no matching merge deadline/ownership checks.
4. Receipt-failure rollback was best effort; failed row deletion or interrupted
   persistence could permit another MediaRecord on retry.
5. Status trusted the receipt owner without checking the durable record owner;
   cleanup failure left active metadata usable by late chunks.
6. Legacy status handling deleted receipts lacking session shape, discarding
   valid business idempotency information.
7. Retry, concurrency, confirmation ordering and partial-failure coverage was
   insufficient for the final target.

## 5. Changes made

| Files | Behavior |
| --- | --- |
| `application/ports/ingest.py`, `application/ports/media.py` | Explicit confirmed-index, lease-refresh, deterministic-source and committed-result lookup contracts. |
| `infrastructure/redis.py` | Redis Set; atomic guarded confirmation and paired TTL renewal; one-time legacy migration detection; redis-py Lock and token-safe refresh/release; upload Redis outages map to typed storage failure. |
| `infrastructure/media/uploads.py` | Set-based status/exact completeness; no delete after uncertain Redis confirmation; bounded protected merge; ownership checks; deterministic final source and committed-row recovery; retain receipt; bounded cleanup after core success. |
| `infrastructure/media/memory.py` | Equivalent confirmation/lookup contracts for offline tests. The explicit local API profile remains process-local. |
| `infrastructure/storage.py` | Stable MinIO source identity for a deterministic object key. |
| `infrastructure/persistence/media_repository.py`, `sqlalchemy.py` | Exact-source lookup in SQLite/DB-API/production SQLAlchemy; detect multiple matching SQL rows instead of guessing; bound production PyMySQL connect/read/write I/O. No schema change or temporary SQL progress writes. |
| `client/src/chunkUpload.test.js` | Behavioral retry/cancellation/concurrency/resume tests; uploader policy unchanged. |
| `tests/infrastructure/test_uploads_3c.py`, `test_persistence_8d.py` | Ordering, exact indexes, concurrency, deadline, ownership, late confirmation, partial commit/receipt recovery and lookup regressions. |
| `tests/infrastructure/test_upload_redis_minio_live.py`, `scripts/run_upload_live_tests.py` | Opt-in real Redis/MinIO/MySQL and production HTTP verification; generated users/UUIDs and scoped cleanup. |
| Architecture, interview guide, R6 upload addenda and these audit/report documents | Describe the actual production behavior and supersede historical upload rollback claims. |

Paths above are relative to `src/dovideo` for application/infrastructure source.
Dependency versions, deployment credentials and Compose topology are unchanged.

## 6. Redis model

| Key | Type / purpose | TTL |
| --- | --- | --- |
| `upload:session:{uuid}` | JSON UUID, user, filename, totalChunks, createdAt/expiresAt/state and migration flag | Approximately 24h, renewed on confirmation and accepted merge |
| `upload:parts:{uuid}` | Set of persisted-and-confirmed numeric indexes | Renewed atomically with active metadata |
| `lock:upload-merge:{uuid}` | redis-py Lock, unique owning token | 30min; nonblocking acquisition; token-checked refresh/release |
| `upload:completed:{uuid}` | JSON UUID → mediaId, user and original session shape | Approximately 24h after completion |

No binary bytes are stored in Redis. Fresh sessions never promote unconfirmed
MinIO objects to progress. Only pre-Set metadata is reconciled once from the
existing deterministic object namespace, with an active/receipt guard.

## 7. Failure semantics

| Failure | Result and retry behavior |
| --- | --- |
| MinIO chunk write fails | No Redis confirmation; retry that chunk. |
| MinIO succeeds / Redis confirmation fails or ACK is lost | Keep deterministic object and any existing confirmed state. Status reports only confirmed indexes. Retry overwrites the same key and confirms it idempotently. |
| Upload Redis unavailable | Typed 503; preserve browser credential; no local fallback. |
| Merge read/final write fails or exceeds deadline | Release owning lock, retain valid chunks/coordination, allow complete retry. A final write with no row can leave an object that is reused/overwritten by the same UUID. |
| MySQL save fails or commit ACK is uncertain | Retain deterministic final bytes and chunks. Retry looks for an already committed row first; otherwise merge/save can retry. A cancelled DB save settles under the lease before cancellation propagates. |
| MySQL row committed / receipt creation fails | Return retryable failure, preserve row/object/chunks. Next complete finds that exact row and repairs the receipt without another merge/insert. No rollback dependency or heavyweight transaction. |
| Completion HTTP response lost | Status and repeated complete return the same media ID; no second merge or row. |
| Chunk/session/workspace cleanup fails or chunk cleanup times out | Core success stays successful. Receipt remains available; late chunks are rejected even if metadata survived cleanup. |
| Receipt/active coordination expires or Redis loses all state | Recovery window ends; explicit missing/expired response, never invent process-local state. |

Merge scope deadline is 20min, shorter than the 30min Redis lease. Library
`reacquire()` runs at chunk/persistence boundaries; there is no periodic watchdog.
PyMySQL connect uses pool timeout (3s by default), read/write use 30s. Cleanup
has a separate 10s budget outside the merge deadline. Arbitrarily paused
processes or Redis failover beyond the lease, ambiguous external I/O after
process death, and orphan object reclamation remain operational limits.

Legacy receipts without session shape are retained. Status can recover the
original shape from surviving metadata; otherwise it returns a conflict while
direct complete can still recover the recorded result. No fabricated chunk count.

## 8. Idempotency

**Merge Lock = concurrent mutual exclusion.** Same UUID cannot merge concurrently
while its Redis lease is valid. Different UUIDs are independent.

**Completion Receipt = business idempotency.** Later complete calls return the
recorded media ID rather than executing merge again. It is rechecked inside the
protected flow, with both receipt and durable row ownership enforced.

The deterministic final source adds recovery for the gap between MySQL commit
and Redis receipt. It derives from upload UUID, not content hash, and does not
deduplicate different attempts or different users. Existing duplicate SQL
sources fail closed. This is bounded retry convergence, not exactly-once or a
cross-storage transaction guarantee.

## 9. Tests

Commands run from the isolated repository, except npm/Node commands from `client`.
Python runtime: `D:\Agent Learning\dovideo-python\.venv\Scripts\python.exe`
(Python 3.12.14, redis-py 7.4.1); Node 24.15.0; npm 11.12.1.

| Exact command | Result |
| --- | --- |
| `& 'D:\Agent Learning\dovideo-python\.venv\Scripts\python.exe' -m pytest -q` | 935 passed; 9 opt-in live tests skipped in this offline invocation; one pre-existing Starlette/httpx deprecation warning. |
| `& 'D:\Agent Learning\dovideo-python\.venv\Scripts\python.exe' -m pytest tests/infrastructure/test_uploads_3c.py tests/presentation/test_r6_upload_api.py tests/infrastructure/test_persistence_8d.py -q` | 39 passed; same warning. |
| `& 'D:\Agent Learning\dovideo-python\.venv\Scripts\python.exe' scripts/run_upload_live_tests.py --env-file 'D:\Agent Learning\dovideo-python\.env.r2.local'` | 9 passed against real Redis 7, MinIO and MySQL 8; same warning. |
| `npm.cmd ci --cache 'D:\Agent Learning\.npm-cache'` | 38 packages installed; lockfile unchanged. |
| `npm.cmd test` | 79 passed, zero skipped/failures. |
| `node --test src/chunkUpload.test.js` | 27 passed. |
| `npm.cmd run build` | rolldown-vite 7.2.5 production build passed. |
| `& 'D:\Agent Learning\dovideo-python\.venv\Scripts\python.exe' -m compileall -q src scripts tests` | Passed. |
| `git diff --check` | Passed. |

No separate lint/type scripts are configured. The full backend run includes
domain/application, infrastructure adapter/integration and presentation tests.
The nine skipped live tests were separately enabled and all passed; they were
not substituted by mocks. Failure injection surrounds actual storage operations.
Real verification includes three concurrent full 5 MiB parts plus a final short
part, final byte ordering/hash, cross-client locks/lease expiry, receipt failure
after MySQL commit, new-facade recovery, ownership, HTTP lost response, Redis
outage, migration and confirmation after expiry/completion. Test IDs/users/objects
are generated; scoped teardown removes them. No provider calls or credentials
are printed or committed.

Intermediate issues were corrected before the final gate: an HTTP test harness
used a nonexistent auth helper (replaced by real register/login), and default
Windows `core.autocrlf=true` changed the frozen v1 dataset bytes in the clone.
The latter caused one unrelated existing hash regression. The file was restored
byte-for-byte from the Git blob (SHA256
`e318193a681ed2794808713a4d1a9dd723b429ad030aee66ecd1bdcde488c2bc`), with a
clone-local `.git/info/attributes` override and index stat refresh. Dataset Git
blob is unchanged; no dataset/test-expectation change is in the commit.

## 10. Non-goals preserved

No Redisson/Java dependency, pre-upload MD5, hash-based upload ID, instant
upload, cross-user deduplication, server-side compose, dynamic chunks, SQL
per-chunk dual writes, watchdog, custom transaction framework or unrelated
architecture rewrite. Existing frontend policy and application Ports are kept.

## 11. Git

One focused commit is authorized only after all final checks above pass, then
a normal fast-forward push to `origin/main` and remote/local HEAD equality
verification. No history rewrite, force push, release, tag or deployment.
The final chat delivery records the resulting commit hash and remote check;
this tracked report cannot embed its own future commit hash.

## 12. Interview-ready architecture

UUID identifies one upload attempt. Redis holds short-lived session metadata,
confirmed chunk indexes and a per-upload distributed merge lock; MinIO holds
chunk and final bytes. The browser resumes from server-confirmed indexes.
Completion checks ownership/exact indexes, merges in order while hashing,
stores the final object and durable MySQL media record, and writes a separate
Redis receipt mapping uploadId to mediaId. The lock excludes concurrent merge;
the receipt converges repeated/lost-response completion to one result. Durable
source lookup repairs a missing receipt; temporary cleanup is best effort.

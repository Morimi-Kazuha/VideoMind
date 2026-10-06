# Upload finalization source audit (2026-10-06)

Baseline: remote `main` and isolated checkout both
`30504e52352a7eb94bb98efc9db17d865f21c760`; isolated tree clean.
The original `dovideo-python` checkout has unrelated uncommitted work and is
not edited. Baseline upload infrastructure/API tests: 17 passed.

Read before implementation: client chunk uploader/tests/API and App upload
actions; application media values and ingest/media ports; upload orchestration,
memory adapters, Redis adapters, MinIO adapters; SQLite/DB-API and production
SQLAlchemy media repositories; R2 configuration/bootstrap/facade and API error
handlers/routes; upload/API/ingest/media/persistence tests; pyproject, client
package and lockfile, Compose/environment template, R2 live smoke; architecture,
interview guide and R6 audit/reliability/precommit upload sections.

| CURRENT | TARGET | GAP | ACTION |
| --- | --- | --- | --- |
| UUID sessions, 24h expiry, deterministic part keys | Separate attempt identity from content hash | None | Retain and verify |
| Browser 5 MiB, concurrency 3, max 410 chunks, 4 attempts with exponential jitter; scoped credential and server status | Bounded upload, transient-only retry, honest resume | Test coverage for retry/backoff/concurrency/cancellation/dead sessions is sparse | Add behavioral regressions; keep policy |
| Status/completeness list MinIO objects; chunk PUT then unconditional Redis renewal | Redis Set of confirmed indexes after object persistence | No Set; renewal rollback can remove a previously valid part; late renewal can recreate active metadata | Atomic guarded confirmation plus Set and shared expiry; retain object on uncertain Redis failure |
| Production already wires Redis merge lock using SET NX PX + custom Lua release, fixed 30min TTL | Mature Python Redis lock per upload, bounded lease safety | Library primitive unused; whole merge duration unbounded; synchronous Redis runs on event loop | Use redis-py Lock, move calls off loop, bounded merge scope and ownership refresh at merge/persistence boundaries |
| Ordered local merge hashes flowing bytes; compares exact indexes | Same | None | Retain |
| Completion receipt checked under lock; rollback deletes final object and row if receipt fails | Same media after lost response; no duplicate after partial failure | Rollback is best effort: row deletion failure permits another row on retry; process interruption has the same gap | Deterministic final object source per UUID; recover committed row by exact source before merging, retain durable result on receipt failure, retry receipt |
| Complete checks receipt and row owner, status checks only receipt owner | All status/chunk/complete ownership enforced | Corrupt receipt can reveal foreign media ID through status; surviving active metadata permits late chunks after cleanup failure | Check durable row owner in status; reject completed receipts before active metadata |
| Cleanup after receipt is best effort | Successful core result survives cleanup failure | None in normal flow | Preserve and add failure tests |
| Docker services healthy; sandbox lacks Docker pipe access | Real Redis/MinIO/MySQL verification | Ordinary sandbox access denied | Read-only elevated Docker check succeeded; use existing services and isolated test identities |

Keep Ports/Adapters: they isolate test fakes and production dependencies.
No new storage service, Java client, pre-upload hash, deduplication, compose,
dynamic chunks, MySQL per-chunk writes, watchdog or transaction framework.

Recovery is bounded by upload/receipt TTL and durable media existence. Redis
loss is a coordination outage, not an excuse to fall back to process-local
state. Existing active sessions created before Set deployment need a guarded,
one-time object-list reconciliation; fresh sessions must never infer progress
from unconfirmed objects.

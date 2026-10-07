# Long-video retrieval source audit

Baseline: `main`, `c7c02707911c8cb823d7f7a46c328469689256b7`.
Initial worktree: only untracked `docs/interview-guide/` (user work, excluded).

## CURRENT

- `VideoChunkingService` assigns by segment start to fixed 300,000 ms buckets.
- Embeddings contain summary + newline + normalized keywords; source revision
  is independent of the chunking contract. Canonical evidence remains segments.
- Qdrant returns six scores filtered only by media; the application then scores
  every loaded chunk, using local cosine for missing remote hits.
- Chunk scores combine dense/keyword/OCR with .60/.25/.15; top three parents
  expand to segment scoring (.55/.25/.20), without overlap deduplication.
- Long context reuses any nonempty chunk checkpoint. Short-context selection
  bypasses chunk construction. Evidence verification separately checks source
  items, revision, timestamps and quote support.
- Strict R4 detects provider/vector fallbacks, unlike resilient base services.
- X3 already owns immutable cases, provenance projections, metrics and artifact
  writing. AgentLoopEvaluationAdapter leaves retrieved_evidence empty.
- Offline composition uses local embedding/planning and InMemoryVectorIndex;
  its hits currently omit chunk identity and revision.

## TARGET

Canonical segments -> 5 minute windows / 1 minute overlap -> independently
ranked dense and BM25 candidates -> RRF -> configured cross-encoder reranking
-> top three parents -> bounded rank-derived segment relevance -> identity
deduplication -> provenance hits -> unchanged evidence verification.

## GAP / DECISIONS

1. Centralize sliding window planning; stop once the tail is covered, skip
   empty gaps, retain whole canonical segments intersecting a window.
2. Validate loaded checkpoints against the deterministic current window plan
   and the actual segment universe, without changing storage schema.
3. Add optional source/version scope to vector search before candidate limiting.
   Keep legacy unscoped reads; never delete all media points on normal requests.
4. Replace partial score lookup with a true candidate arm. Healthy empty search
   stays empty; only unavailable/search-error/local mode permits local cosine.
5. Separate document/tokenization, BM25, fusion and provider adapter modules.
   Keep summary-only embedding representation and existing query planner.
6. Add a dedicated, disabled-by-default reranker config and a documented real
   transport; keep strict R4 fallback rejection and cancellation/budget escape.
7. Reuse X3 projections/metrics via a bounded evaluation-only retrieval adapter.
   Compare against source frozen from this Git baseline, with synthetic fixtures
   clearly identified and historical golden datasets/reports unchanged.

Intentional test replacements: fixed bucket timestamps, six-vector lookup,
partial remote-score override, raw chunk weights, and private _vector_scores
strict tests. Preserve provider, evidence, budgeting and lifecycle contracts.

# Long-video hybrid retrieval

Canonical 60s segments remain the evidence unit. Chunks are five-minute coarse
windows, with one-minute overlap / four-minute stride. Empty gaps are skipped;
the first window whose end covers the final segment stops generation, avoiding
redundant tail summaries. Whole segments intersecting windows are shared.

The version is `video-chunk-5m-overlap1m-v2`. Source revision and preprocessing
reuse are unchanged. Checkpoint compatibility requires current version, current
ID, exact range/order/window coverage and the same segment payloads/revision.
Old/incomplete/foreign payloads are misses, rebuilt and reindexed without DDL.
Chunk payload deserialization failures are also misses; storage read failures
and invalid authoritative VideoContext checkpoints still propagate.

## Candidate recall and fusion

Dense: existing summary + newline + keywords embeddings, production BGE-M3,
Qdrant Top8. Search scope uses media + source revision + chunking version,
before the candidate limit. Normal searches never locally cosine non-hits.
Healthy empty search is empty; local composition or failed/unavailable vector
paths permit local cosine over current loaded chunks.

Sparse: request-local Okapi BM25 (`k1=1.2`, `b=.75`), Top8 over normalized,
distinct summary/ASR/OCR sentences and keywords absent from that text. Repeated
terms within a sentence retain TF; duplicate channels/sentences do not inflate
it. NFKC/case normalization, Chinese unigrams/bigrams and Latin/numeric/technical
compound tokens require no new runtime dependency.

RRF sums `1 / (60 + one-based rank)` independently for both arms, deduplicates
each arm, caps the union to 10 and breaks ties by corpus index. Either arm may
be empty. Raw dense/BM25 scores never mix across arms.

## Configured precision stage

The provider-neutral RerankerPort receives candidate ID + bounded document text
(at most 12,000 characters each). The SiliconFlow adapter implements its
[documented text contract](https://docs.siliconflow.cn/docs/api/rerank-post):
query, documents, model, top_n and return_documents=false, with index /
relevance_score responses mapped back to candidate identities. All supplied
candidate indices must appear exactly once, with finite scores.

Default: disabled, RRF -> Final3. Enabled: RRF Top10 -> configured cross-encoder
-> Final3. Model target: `BAAI/bge-reranker-v2-m3`. Independent settings:
`DOVIDEO_RERANKER_ENABLED`, `_URL`, `_API_KEY`, `_MODEL`, `_TIMEOUT_SECONDS`,
`_MAX_ATTEMPTS`, `_RETRY_DELAY_SECONDS`. Defaults: 20 seconds per attempt,
two attempts; HTTP 408/429/5xx and OSError transport failures retry, auth/request/malformed
responses fail immediately. Timeout/cancellation retain their identity.
Live BGE-M3/Qdrant/reranker validation for this change: **NOT_RUN**.

## Segment ranking and reliability

Final parent ordering maps to bounded `1 / parent_rank` relevance. Segment
score is `.55 * parent_relevance + .25 * ASR_match + .20 * OCR_match`, where
each lexical signal is a distinct normalized matched-term fraction in [0,1].
No BM25/RRF/reranker absolute score enters this formula. Sort descending,
then timestamp and source order. This explicit policy is conservative but has
a measured weakness: a high-ranked parent can dominate better text in another
parent. It is not a calibrated relevance probability.

Dedup identity is segment_id; legacy identity hashes source revision, start/end,
transcript, OCR and frame references. Keep the best parent contribution; exact
ties keep the first path. Same text at different times remains distinct.
EvidenceHit retains source_revision, selected parent chunk_id, segment_id and
source_item_ids. Existing EvidenceVerificationService remains the authority.

| Condition | Base application | Canonical Strict R4 |
| --- | --- | --- |
| Qdrant unavailable/error | local cosine + BM25; vectorStoreFallbacks | typed vector failure |
| Healthy empty Qdrant result | empty dense arm, BM25 continues | no vector fallback; no-evidence may still fail |
| Embedding failure | BM25-only; embeddingFallbacks | provider failure |
| BM25 internal failure | dense-only; sparseFallbacks | canonical failure |
| Reranker disabled | RRF order, no failure counter | accepted configured path |
| Enabled reranker failure | RRF order; rerankerFallbacks | canonical provider failure |
| Cancellation / budget exceeded | propagate | propagate |

Telemetry records candidate/segment counts and fallback counters. The obsolete
retrievalTopScore metric is removed. Query, ASR/OCR text and keys are excluded.

## Evaluation and measured limits

Reuse X3 EvaluationCase/Dataset, EvaluationRunner, EvaluationArtifactWriter and
calculate_retrieval_metrics. A transient ContextVar observes the initial actual
user-query ranking during AgentLoop evaluation; later Critic/tool queries are
not merged into that ranking. EvaluationRetrievedEvidence keeps one candidate
per segment, including all source item identities and ASR+OCR modality, without
raw text. Retrieval-only runner mode leaves answer/guard metrics unmeasured.

`retrieval-focused-v2` has 12 SYNTHETIC cases (30-minute source fixture, six
categories). Both arms use the same local TF-IDF/summary/planner. Baseline loads
exact chunking/retrieval/provenance source from commit c7c0270 in evaluation
only; production has one current retrieval implementation. Two repeated
comparisons produce identical metric/category/structural results.

| Metric | Baseline | Final (reranker disabled) |
| --- | ---: | ---: |
| Recall@1 | .833333 | .750000 |
| Recall@3 | 1.000000 | .833333 |
| Recall@5 | 1.000000 | .833333 |
| Precision@1 | .916667 | .833333 |
| Precision@3 | .388889 | .333333 |
| Precision@5 | .233333 | .200000 |
| MRR | .958333 | .861111 |
| Temporal hit / coverage | 1.000 / 1.000 | 1.000 / 1.000 |

Final Recall@3 is 1.0 for exact terms, OCR-only, ASR-only and boundary cases;
0.5 for paraphrase and distractor categories. Those missed Top5 segments remain
in the eight search hits (rank six in the failing cases), so temporal coverage
over the full hit list is 1.0. It is not temporal coverage@K. Local TF-IDF is not
a pretrained semantic model; no live quality/performance gain is established.
No-answer/abstention behavior is NOT_MEASURED. These findings do not justify a
quality-improvement claim or automatic production promotion.

Run `python tools/run_retrieval_comparison.py --output work/new-comparison`.
The output must be empty to preserve earlier experiments. Sanitized committed
artifacts are in [retrieval-evaluation-v2](retrieval-evaluation-v2/comparison.json).
The exploratory v1 fixture and results are retained separately; v2 corrects a
source-time mismatch in its second boundary case. See the [dataset version
record](../datasets/x3/RETRIEVAL_DATASETS.md).
Historical golden datasets and X3 reports remain unchanged. Semantic chunking,
dynamic TopK, neighbor expansion and cross-video search remain outside scope.

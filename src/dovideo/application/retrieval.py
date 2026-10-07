"""Dense/BM25 candidate retrieval, RRF, optional reranking and video evidence."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from dovideo.domain import (
    VideoChunk,
    VideoEvidenceHit,
    VideoRetrievalIntent,
    VideoSegment,
)

from .errors import BudgetExceededError
from .ports.ai import EmbeddingPort, RetrievalPlannerPort
from .ports.observability import TelemetryPort
from .ports.retrieval import VectorIndexPort, RerankerDocument, RerankerPort
from .value_objects import VectorHit
from .rank_fusion import RankedCandidate, reciprocal_rank_fusion
from .retrieval_documents import retrieval_document, segment_identity, normalize_text
from .sparse_retrieval import BM25Retriever
from .retrieval_observation import observe_retrieval


DENSE_CANDIDATE_K = 8
SPARSE_CANDIDATE_K = 8
FUSION_CANDIDATE_K = 10
FINAL_CHUNK_K = 3
TOP_K = FINAL_CHUNK_K
MAX_USER_HITS = 8
MAX_SNIPPET_LENGTH = 180
VECTOR_LOOKUP_LIMIT = DENSE_CANDIDATE_K
SEGMENT_CHUNK_WEIGHT = 0.55
SEGMENT_TRANSCRIPT_WEIGHT = 0.25
SEGMENT_VISUAL_WEIGHT = 0.20


class VideoEvidenceRetrievalService:
    """Rank raw context segments and expose directly seekable evidence hits."""

    def __init__(
        self,
        retrieval_planner: RetrievalPlannerPort | None = None,
        embedding: EmbeddingPort | None = None,
        vector_index: VectorIndexPort | None = None,
        telemetry: TelemetryPort | None = None,
        *,
        planner: RetrievalPlannerPort | None = None,
        embedder: EmbeddingPort | None = None,
        vector_store: VectorIndexPort | None = None,
        sparse: BM25Retriever | None = None,
        reranker: RerankerPort | None = None,
    ) -> None:
        """Create the service from provider-neutral retrieval boundaries.

        The keyword aliases make migration wiring readable while keeping the
        canonical names aligned with the application port types.
        """

        self._retrieval_planner = (
            retrieval_planner if retrieval_planner is not None else planner
        )
        self._embedding = embedding if embedding is not None else embedder
        self._vector_index = vector_index if vector_index is not None else vector_store
        self._telemetry = telemetry
        self._sparse = sparse if sparse is not None else BM25Retriever()
        self._reranker = reranker
        if self._retrieval_planner is None:
            raise TypeError("retrieval_planner is required")
        if self._embedding is None:
            raise TypeError("embedding is required")
        if self._vector_index is None:
            raise TypeError("vector_index is required")

    async def retrieve(
        self,
        media_id: int | None,
        goal: str | None,
        chunks: Iterable[VideoChunk],
    ) -> tuple[VideoSegment, ...]:
        """Return unique ranked segments from the final three candidate chunks."""

        ranked_segments = await self._rank(media_id, goal, chunks)
        observe_retrieval(lambda: tuple(self._to_hit(scored) for scored in ranked_segments))
        return tuple(scored.segment for scored in ranked_segments)

    async def search(
        self,
        media_id: int | None,
        query: str | None,
        chunks: Iterable[VideoChunk],
    ) -> tuple[VideoEvidenceHit, ...]:
        """Return at most eight ranked, directly seekable evidence hits."""

        ranked_segments = await self._rank(media_id, query, chunks)
        hits = tuple(
            self._to_hit(scored) for scored in ranked_segments[:MAX_USER_HITS]
        )
        observe_retrieval(lambda: hits)
        return hits

    async def index(
        self,
        media_id: int | None,
        chunks: Iterable[VideoChunk],
    ) -> None:
        """Best-effort vector indexing; retrieval does not depend on it."""

        # Copy before handing the sequence to an adapter so callers retain
        # ownership and Java's ``chunks.size()`` metric remains exact.
        normalized = tuple(chunks)
        try:
            await self._vector_index.upsert(media_id, normalized)
            self._increment("vectorStoreWrites", len(normalized))
        except BudgetExceededError:
            raise
        except Exception:
            # Cancellation is a BaseException and intentionally propagates.
            self._increment("vectorStoreFallbacks")

    async def _rank(
        self,
        media_id: int | None,
        goal: str | None,
        chunks: Iterable[VideoChunk],
    ) -> tuple[_ScoredSegment, ...]:
        intent = await self._retrieval_intent(goal)
        query_embedding = await self._embed(intent.semantic_query)
        # Materialize once; no sorting operation mutates the caller's list.
        normalized_chunks = tuple(chunks)
        documents = tuple(retrieval_document(chunk) for chunk in normalized_chunks)
        dense = await self._dense_candidates(media_id, query_embedding, normalized_chunks)
        try:
            sparse = self._sparse.rank(
                " ".join((intent.semantic_query, *intent.keywords, *intent.visual_keywords)),
                documents, limit=SPARSE_CANDIDATE_K,
            )
        except BudgetExceededError:
            raise
        except Exception:
            self._increment("sparseFallbacks")
            sparse = ()
        fused = reciprocal_rank_fusion((dense, sparse), limit=FUSION_CANDIDATE_K)
        self._observe("denseCandidates", len(dense))
        self._observe("sparseCandidates", len(sparse))
        self._observe("fusionCandidates", len(fused))
        ranked = await self._rerank(intent.semantic_query, fused, documents)
        selected = ranked[:FINAL_CHUNK_K]
        self._increment("retrievalChunks", len(selected))

        by_identity: dict[str, _ScoredSegment] = {}
        segment_order = 0
        for parent_rank, candidate in enumerate(selected, start=1):
            chunk = normalized_chunks[candidate.index]
            # Only ordering crosses provider boundaries, never absolute scores.
            parent_relevance = 1.0 / parent_rank
            for segment in chunk.raw_segments:
                transcript_score = _term_score(
                    intent.keywords, segment.transcript
                )
                visual_score = _term_score(
                    intent.visual_keywords,
                    " ".join(_normalized_ocr_texts(segment)),
                )
                scored = _ScoredSegment(
                    segment=segment,
                    score=(
                        parent_relevance * SEGMENT_CHUNK_WEIGHT
                        + transcript_score * SEGMENT_TRANSCRIPT_WEIGHT
                        + visual_score * SEGMENT_VISUAL_WEIGHT
                    ),
                    transcript_score=transcript_score,
                    visual_score=visual_score,
                    order=segment_order,
                    source_revision=segment.source_revision or chunk.source_revision,
                    chunk_id=chunk.chunk_id,
                )
                identity = segment_identity(segment)
                previous = by_identity.get(identity)
                if previous is None or scored.score > previous.score:
                    by_identity[identity] = scored
                segment_order += 1
        # Java compares score descending, then timestamp ascending.  Retain
        # source order for exact ties (including duplicate timestamps).
        ranked_segments = sorted(
            by_identity.values(),
            key=lambda item: (-item.score, item.segment.start_ms, item.order)
        )
        self._observe("retrievedSegmentCount", len(ranked_segments))
        return tuple(ranked_segments)

    async def _retrieval_intent(self, goal: str | None) -> VideoRetrievalIntent:
        try:
            intent = await self._retrieval_planner.plan_retrieval(goal or "")
            if not isinstance(intent, VideoRetrievalIntent):
                raise TypeError("retrieval planner returned an invalid intent")
            if intent.semantic_query.strip():
                return intent
        except BudgetExceededError:
            raise
        except Exception:
            self._increment("retrievalIntentFallbacks")

        # A valid intent with a blank semantic query follows Java's fallback
        # path without incrementing the exception-only fallback metric.
        terms = fallback_terms(goal)
        return VideoRetrievalIntent(
            semantic_query=goal or "",
            keywords=terms,
            visual_keywords=terms,
        )

    async def _embed(self, text: str) -> tuple[float, ...]:
        try:
            value = await self._embedding.embed(text)
            if value is None:
                raise TypeError("embedding port returned None")
            vector = tuple(value)
            if text.strip() and (not vector or any(not math.isfinite(float(v)) for v in vector)):
                raise ValueError("embedding vector unavailable")
            return vector
        except BudgetExceededError:
            raise
        except Exception:
            self._increment("embeddingFallbacks")
            return ()

    async def _dense_candidates(
        self,
        media_id: int | None,
        query_embedding: tuple[float, ...],
        chunks: Sequence[VideoChunk] = (),
    ) -> tuple[RankedCandidate, ...]:
        if not query_embedding:
            return ()
        if media_id is None:
            return self._local_dense(query_embedding, chunks)
        try:
            if not getattr(self._vector_index, "enabled", True):
                raise RuntimeError("vector index unavailable")
            scope = vector_scope(chunks)
            hits = await self._vector_index.search(
                media_id,
                query_embedding,
                limit=DENSE_CANDIDATE_K,
                **scope,
            )
            scores: dict[int, float] = {}
            lookup = {c.chunk_id if c.chunk_id else _range_key(c.start_ms, c.end_ms): i
                      for i, c in enumerate(chunks)}
            for hit in hits:
                if not isinstance(hit, VectorHit) or not math.isfinite(hit.score):
                    continue
                if scope.get("source_revision") is not None and hit.source_revision != scope["source_revision"]:
                    continue
                if scope.get("chunking_version") is not None and hit.chunking_version != scope["chunking_version"]:
                    continue
                key = hit.chunk_id if hit.chunk_id else _range_key(hit.start_ms, hit.end_ms)
                index = lookup.get(key)
                if index is None:
                    continue
                chunk = chunks[index]
                if (hit.start_ms, hit.end_ms) != (chunk.start_ms, chunk.end_ms):
                    continue
                scores[index] = max(scores.get(index, -math.inf), hit.score)
            return tuple(sorted((RankedCandidate(i, s) for i, s in scores.items()),
                                key=lambda c: (-c.score, c.index))[:DENSE_CANDIDATE_K])
        except BudgetExceededError:
            raise
        except Exception:
            self._increment("vectorStoreFallbacks")
            return self._local_dense(query_embedding, chunks)

    def _local_dense(self, embedding, chunks):
        return tuple(sorted((RankedCandidate(i, cosine_similarity(embedding, c.embedding))
                             for i, c in enumerate(chunks) if c.embedding),
                            key=lambda c: (-c.score, c.index))[:DENSE_CANDIDATE_K])

    async def _rerank(self, query, candidates, documents):
        if self._reranker is None or not candidates:
            self._observe("rerankedCandidates", 0)
            return candidates
        try:
            inputs = tuple(RerankerDocument(str(c.index), documents[c.index][:MAX_RERANKER_DOCUMENT_CHARS])
                           for c in candidates)
            results = await self._reranker.rerank(query, inputs)
            scores = {r.candidate_id: r.score for r in results}
            if (len(results) != len(candidates) or len(scores) != len(candidates)
                or set(scores) != {d.candidate_id for d in inputs}
                or any(isinstance(s, bool) or not math.isfinite(float(s)) for s in scores.values())):
                raise ValueError("reranker returned an invalid candidate permutation")
            result = tuple(sorted(candidates, key=lambda c: -scores[str(c.index)]))
            self._observe("rerankedCandidates", len(result))
            return result
        except BudgetExceededError:
            raise
        except Exception:
            self._increment("rerankerFallbacks")
            self._observe("rerankedCandidates", 0)
            return candidates

    def _to_hit(self, scored: _ScoredSegment) -> VideoEvidenceHit:
        segment = scored.segment
        ocr_texts = _normalized_ocr_texts(segment)
        has_transcript = bool(segment.transcript.strip())
        has_ocr = bool(ocr_texts)
        source = (
            "ASR+OCR"
            if has_transcript and has_ocr
            else "OCR"
            if has_ocr
            else "ASR"
            if has_transcript
            else "时间片段"
        )
        ocr_text = " ".join(ocr_texts)
        preferred = (
            ocr_text
            if scored.visual_score > scored.transcript_score
            else segment.transcript
        )
        if not preferred.strip():
            preferred = ocr_text if has_ocr else segment.transcript
        if not preferred.strip():
            preferred = "该时间段暂无可展示文本"
        return VideoEvidenceHit(
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            source=source,
            snippet=abbreviate(preferred),
            transcript=segment.transcript,
            ocr_texts=ocr_texts,
            source_revision=scored.source_revision,
            chunk_id=scored.chunk_id,
            segment_id=segment.segment_id,
            source_item_ids=segment.source_item_ids,
        )

    def _increment(self, metric: str, amount: int = 1) -> None:
        if self._telemetry is not None:
            self._telemetry.increment(metric, amount)

    def _observe(self, metric: str, value: float) -> None:
        if self._telemetry is not None:
            self._telemetry.observe(metric, value)


@dataclass(frozen=True, slots=True)
class _ScoredSegment:
    segment: VideoSegment
    score: float
    transcript_score: float
    visual_score: float
    order: int
    source_revision: str = ""
    chunk_id: str = ""


def _range_key(start_ms: int, end_ms: int) -> str:
    return f"{start_ms}:{end_ms}"


MAX_RERANKER_DOCUMENT_CHARS = 12_000


def vector_scope(chunks: Sequence[VideoChunk]) -> dict[str, str]:
    """Scope before limiting candidates; mixed revisions/versions are invalid."""
    scope = {}
    for field in ("source_revision", "chunking_version"):
        values = {getattr(c, field) for c in chunks}
        if len(values) > 1:
            raise ValueError("mixed chunk search scope")
        if values and (value := next(iter(values))):
            scope[field] = value
    return scope


def _normalized_ocr_texts(segment: VideoSegment) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in segment.ocr_texts:
        if value is None or not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _term_score(terms: Sequence[str], content: str) -> float:
    normalized_content = normalize_search_text(content)
    normalized_terms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = normalize_search_text(term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_terms.append(normalized)
    if not normalized_terms:
        return 0.0
    return sum(term in normalized_content for term in normalized_terms) / len(
        normalized_terms
    )


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity, with Java-compatible zero safeguards."""

    if len(left) != len(right) or not left:
        return 0.0
    try:
        dot = sum(a * b for a, b in zip(left, right))
        left_length = sum(value * value for value in left)
        right_length = sum(value * value for value in right)
        if left_length == 0 or right_length == 0:
            return 0.0
        result = dot / (math.sqrt(left_length) * math.sqrt(right_length))
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


_TERM_SPLIT = re.compile(r"[\s，。！？、,.;:：；!?]+")
_WHITESPACE = re.compile(r"\s+")


def fallback_terms(query: str | None) -> tuple[str, ...]:
    """Apply Java's punctuation split and bounded fallback term policy."""

    if query is None or not query.strip():
        return ()
    terms = [
        term.strip()
        for term in _TERM_SPLIT.split(query.strip())
        if len(term.strip()) >= 2
    ]
    distinct: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term in seen:
            continue
        seen.add(term)
        distinct.append(term)
        if len(distinct) == 8:
            break
    return tuple(distinct) if distinct else (query.strip(),)


def normalize_search_text(value: str | None) -> str:
    """Lowercase Unicode text and remove all Unicode whitespace."""

    return _WHITESPACE.sub("", normalize_text(value or ""))


def abbreviate(value: str | None) -> str:
    normalized = _WHITESPACE.sub(" ", value or "").strip()
    if len(normalized) <= MAX_SNIPPET_LENGTH:
        return normalized
    return normalized[:MAX_SNIPPET_LENGTH] + "..."


__all__ = [
    "DENSE_CANDIDATE_K",
    "SPARSE_CANDIDATE_K",
    "FUSION_CANDIDATE_K",
    "FINAL_CHUNK_K",
    "MAX_SNIPPET_LENGTH",
    "MAX_USER_HITS",
    "SEGMENT_CHUNK_WEIGHT",
    "SEGMENT_TRANSCRIPT_WEIGHT",
    "SEGMENT_VISUAL_WEIGHT",
    "TOP_K",
    "VECTOR_LOOKUP_LIMIT",
    "VideoEvidenceRetrievalService",
    "abbreviate",
    "cosine_similarity",
    "fallback_terms",
    "normalize_search_text",
]

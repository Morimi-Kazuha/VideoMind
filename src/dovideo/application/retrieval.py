"""Hybrid semantic, keyword, and OCR retrieval for video chunks.

This is the application-level port of Java ``VideoEvidenceRetrievalService``.
Planner, embedding, and vector-index calls remain small injectable ports; a
provider outage therefore falls back to the local cosine and lexical signals.
"""

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

from .ports.ai import EmbeddingPort, RetrievalPlannerPort
from .ports.observability import TelemetryPort
from .ports.retrieval import VectorIndexPort
from .value_objects import VectorHit


TOP_K = 3
MAX_USER_HITS = 8
MAX_SNIPPET_LENGTH = 180
VECTOR_LOOKUP_LIMIT = TOP_K * 2

CHUNK_SEMANTIC_WEIGHT = 0.60
CHUNK_KEYWORD_WEIGHT = 0.25
CHUNK_VISUAL_WEIGHT = 0.15
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
    ) -> None:
        """Create the service from the three provider-neutral ports.

        The keyword aliases make migration wiring readable while keeping the
        canonical names aligned with the application port types.
        """

        self._retrieval_planner = (
            retrieval_planner if retrieval_planner is not None else planner
        )
        self._embedding = embedding if embedding is not None else embedder
        self._vector_index = vector_index if vector_index is not None else vector_store
        self._telemetry = telemetry
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
        """Return every raw segment in the top three ranked chunks."""

        ranked_segments = await self._rank(media_id, goal, chunks)
        return tuple(scored.segment for scored in ranked_segments)

    async def search(
        self,
        media_id: int | None,
        query: str | None,
        chunks: Iterable[VideoChunk],
    ) -> tuple[VideoEvidenceHit, ...]:
        """Return at most eight ranked, directly seekable evidence hits."""

        ranked_segments = await self._rank(media_id, query, chunks)
        return tuple(
            self._to_hit(scored) for scored in ranked_segments[:MAX_USER_HITS]
        )

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
        vector_scores = await self._vector_scores(
            media_id,
            query_embedding,
            normalized_chunks,
        )
        ranked_chunks = [
            _ScoredChunk(
                chunk=chunk,
                score=self._score(intent, query_embedding, vector_scores, chunk),
                order=order,
            )
            for order, chunk in enumerate(normalized_chunks)
        ]
        # Java's ordered stream sort is stable.  The source order tie-breaker
        # makes that behavior explicit and deterministic.
        ranked_chunks.sort(key=lambda item: (-item.score, item.order))
        selected_chunks = tuple(ranked_chunks[:TOP_K])
        if selected_chunks:
            self._observe("retrievalTopScore", selected_chunks[0].score)
            self._increment("retrievalChunks", len(selected_chunks))

        ranked_segments: list[_ScoredSegment] = []
        segment_order = 0
        for scored_chunk in selected_chunks:
            for segment in scored_chunk.chunk.raw_segments:
                transcript_score = _term_score(
                    intent.keywords, segment.transcript
                )
                visual_score = _term_score(
                    intent.visual_keywords,
                    " ".join(_normalized_ocr_texts(segment)),
                )
                ranked_segments.append(
                    _ScoredSegment(
                        segment=segment,
                        score=(
                            scored_chunk.score * SEGMENT_CHUNK_WEIGHT
                            + transcript_score * SEGMENT_TRANSCRIPT_WEIGHT
                            + visual_score * SEGMENT_VISUAL_WEIGHT
                        ),
                        transcript_score=transcript_score,
                        visual_score=visual_score,
                        order=segment_order,
                        source_revision=(
                            segment.source_revision
                            or scored_chunk.chunk.source_revision
                        ),
                        chunk_id=scored_chunk.chunk.chunk_id,
                    )
                )
                segment_order += 1
        # Java compares score descending, then timestamp ascending.  Retain
        # source order for exact ties (including duplicate timestamps).
        ranked_segments.sort(
            key=lambda item: (-item.score, item.segment.start_ms, item.order)
        )
        return tuple(ranked_segments)

    async def _retrieval_intent(self, goal: str | None) -> VideoRetrievalIntent:
        try:
            intent = await self._retrieval_planner.plan_retrieval(goal or "")
            if not isinstance(intent, VideoRetrievalIntent):
                raise TypeError("retrieval planner returned an invalid intent")
            if intent.semantic_query.strip():
                return intent
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
            return tuple(value)
        except Exception:
            self._increment("embeddingFallbacks")
            return ()

    async def _vector_scores(
        self,
        media_id: int | None,
        query_embedding: tuple[float, ...],
        chunks: Sequence[VideoChunk] = (),
    ) -> dict[str, float]:
        if media_id is None or not query_embedding:
            return {}
        try:
            hits = await self._vector_index.search(
                media_id,
                query_embedding,
                limit=VECTOR_LOOKUP_LIMIT,
            )
            scores: dict[str, float] = {}
            expected_chunk_ids = {
                chunk.chunk_id for chunk in chunks if chunk.chunk_id
            }
            expected_revisions = {
                chunk.source_revision for chunk in chunks if chunk.source_revision
            }
            for hit in hits:
                if isinstance(hit, VectorHit):
                    if expected_chunk_ids and hit.chunk_id not in expected_chunk_ids:
                        # A provenance-aware retrieval must not consume a
                        # legacy or different-revision point for the same
                        # media/time range.
                        continue
                    if expected_revisions and (
                        not hit.source_revision
                        or hit.source_revision not in expected_revisions
                    ):
                        continue
                    # Java's LinkedHashMap.put overwrites duplicate ranges;
                    # assigning in iteration order preserves that behavior.
                    if hit.chunk_id:
                        scores[_chunk_id_key(hit.chunk_id)] = float(hit.score)
                    else:
                        scores[_range_key(hit.start_ms, hit.end_ms)] = float(hit.score)
            return scores
        except Exception:
            self._increment("vectorStoreFallbacks")
            return {}

    def _score(
        self,
        intent: VideoRetrievalIntent,
        query_embedding: tuple[float, ...],
        vector_scores: dict[str, float],
        chunk: VideoChunk,
    ) -> float:
        remote_score = (
            vector_scores.get(_chunk_id_key(chunk.chunk_id))
            if chunk.chunk_id
            else None
        )
        if remote_score is None:
            remote_score = vector_scores.get(_range_key(chunk.start_ms, chunk.end_ms))
        semantic_score = (
            remote_score
            if remote_score is not None
            else cosine_similarity(query_embedding, chunk.embedding)
        )
        return (
            semantic_score * CHUNK_SEMANTIC_WEIGHT
            + _term_score(intent.keywords, _searchable_text(chunk))
            * CHUNK_KEYWORD_WEIGHT
            + _term_score(intent.visual_keywords, _visual_text(chunk))
            * CHUNK_VISUAL_WEIGHT
        )

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
class _ScoredChunk:
    chunk: VideoChunk
    score: float
    order: int


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


def _chunk_id_key(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def _searchable_text(chunk: VideoChunk) -> str:
    return " ".join(
        (
            chunk.segment_summary,
            " ".join(chunk.keywords),
            " ".join(segment.transcript for segment in chunk.raw_segments),
        )
    )


def _visual_text(chunk: VideoChunk) -> str:
    return " ".join(
        text
        for segment in chunk.raw_segments
        for text in _normalized_ocr_texts(segment)
    )


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

    return _WHITESPACE.sub("", value or "").lower()


def abbreviate(value: str | None) -> str:
    normalized = _WHITESPACE.sub(" ", value or "").strip()
    if len(normalized) <= MAX_SNIPPET_LENGTH:
        return normalized
    return normalized[:MAX_SNIPPET_LENGTH] + "..."


__all__ = [
    "CHUNK_KEYWORD_WEIGHT",
    "CHUNK_SEMANTIC_WEIGHT",
    "CHUNK_VISUAL_WEIGHT",
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

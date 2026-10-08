"""Long-video context selection and Critic-directed refinement.

The policy is the application-level port of Java
``LongVideoContextService``.  Chunking, retrieval, and checkpoint storage are
injected boundaries; this module contains only selection and budgeting logic.
"""

from __future__ import annotations

from collections.abc import Iterable

from dovideo.domain import (
    CriticResult,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
    VideoSegment,
)

from .chunking import CHUNK_MILLISECONDS, VideoChunkingService, chunks_compatible
from .ports.checkpoint import ContextCheckpointPort
from .ports.observability import TelemetryPort
from .retrieval import VideoEvidenceRetrievalService
from .adaptive_retrieval import baseline_retrieval


CHUNK_MS = CHUNK_MILLISECONDS
MAX_CONTEXT_CHARS = 24_000


class LongVideoContextService:
    """Select relevant segments while preserving Java's budget semantics."""

    def __init__(
        self,
        chunking_service: VideoChunkingService | None = None,
        retrieval_service: VideoEvidenceRetrievalService | None = None,
        checkpoint: ContextCheckpointPort | None = None,
        telemetry: TelemetryPort | None = None,
        *,
        chunking: VideoChunkingService | None = None,
        retrieval: VideoEvidenceRetrievalService | None = None,
    ) -> None:
        self._chunking = chunking_service if chunking_service is not None else chunking
        self._retrieval = (
            retrieval_service if retrieval_service is not None else retrieval
        )
        self._checkpoint = checkpoint
        self._telemetry = telemetry
        if self._chunking is None:
            raise TypeError("chunking_service is required")
        if self._retrieval is None:
            raise TypeError("retrieval_service is required")

    async def select_relevant(
        self,
        context: VideoContext,
        media_id: int | None = None,
    ) -> VideoContext:
        """Select a bounded context, bypassing retrieval for short videos."""

        if not isinstance(context, VideoContext):
            raise TypeError("context must be a VideoContext")
        if (
            not context.segments
            or context.segments[-1].end_ms <= CHUNK_MILLISECONDS
        ):
            return self.within_budget(context, context.segments)

        chunks = await self._resolve_chunks(media_id, context.segments)
        selected = await self._retrieval.retrieve(
            media_id,
            context.user_goal,
            chunks,
        )
        return self.within_budget(context, selected)

    async def search_evidence(
        self,
        media_id: int | None,
        context: VideoContext,
        *,
        chunks: Iterable[VideoChunk] | None = None,
        limit: int | None = None,
    ) -> tuple[VideoEvidenceHit, ...]:
        """Search evidence, optionally reusing already recovered chunks."""

        if not isinstance(context, VideoContext):
            raise TypeError("context must be a VideoContext")
        if not context.segments:
            return ()
        resolved_chunks = (
            tuple(chunks)
            if chunks is not None
            else await self._resolve_chunks(media_id, context.segments)
        )
        if not resolved_chunks:
            return ()
        hits = tuple(
            await self._retrieval.search(
                media_id,
                context.user_goal,
                resolved_chunks,
            )
        )
        if limit is None:
            return hits
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("search evidence limit must be a non-negative integer")
        return hits[:limit]

    async def refine_for_critique(
        self,
        media_id: int | None,
        full_context: VideoContext,
        selected_context: VideoContext,
        critique: CriticResult | None = None,
    ) -> VideoContext:
        """Add Critic-required windows, retry-ranked windows, and prior picks."""

        if not isinstance(full_context, VideoContext):
            raise TypeError("full_context must be a VideoContext")
        if not isinstance(selected_context, VideoContext):
            raise TypeError("selected_context must be a VideoContext")
        if critique is not None and not isinstance(critique, CriticResult):
            raise TypeError("critique must be a CriticResult or None")

        selected_by_key: dict[str, VideoSegment] = {}
        required_timestamps = (
            () if critique is None else tuple(critique.required_timestamps)
        )
        for segment in full_context.segments:
            if any(
                _near_segment(timestamp, segment)
                for timestamp in required_timestamps
            ):
                # Assignment replaces the value but, like Java LinkedHashMap,
                # retains the first insertion position for duplicate keys.
                selected_by_key[_segment_key(segment)] = segment

        query = _critique_query(full_context.user_goal, critique)
        retry_context = VideoContext(
            source=full_context.source,
            user_goal=query,
            segments=full_context.segments,
            source_revision=full_context.source_revision,
            provenance_version=full_context.provenance_version,
        )
        with baseline_retrieval():
            retry_selected = await self.select_relevant(retry_context, media_id)
        for segment in retry_selected.segments:
            selected_by_key.setdefault(_segment_key(segment), segment)
        for segment in selected_context.segments:
            selected_by_key.setdefault(_segment_key(segment), segment)

        # Budgeting uses the original context so the returned goal/source stay
        # stable even though the retry query includes Critic instructions.
        return self.within_budget(full_context, tuple(selected_by_key.values()))

    def within_budget(
        self,
        context: VideoContext,
        candidates: Iterable[VideoSegment],
    ) -> VideoContext:
        """Apply Java's 24,000 UTF-16-code-unit context budget.

        The first candidate is always admitted, even if it exceeds the budget.
        Later overflowing candidates are skipped with ``continue`` so a later
        small candidate can still be admitted.
        """

        if not isinstance(context, VideoContext):
            raise TypeError("context must be a VideoContext")
        ordered_candidates = tuple(candidates)
        selected: list[VideoSegment] = []
        used_chars = 0
        for segment in ordered_candidates:
            segment_chars = _java_string_length(segment.transcript) + sum(
                _java_string_length(text) for text in segment.ocr_texts
            )
            if selected and used_chars + segment_chars > MAX_CONTEXT_CHARS:
                continue
            selected.append(segment)
            used_chars += segment_chars

        self._increment(
            "contextSegmentsDropped",
            len(ordered_candidates) - len(selected),
        )
        self._observe("contextChars", used_chars)
        selected.sort(key=lambda segment: segment.start_ms)
        return VideoContext(
            source=context.source,
            user_goal=context.user_goal,
            segments=tuple(selected),
            source_revision=context.source_revision,
            provenance_version=context.provenance_version,
        )

    async def _resolve_chunks(
        self,
        media_id: int | None,
        segments: Iterable[VideoSegment],
    ) -> tuple[VideoChunk, ...]:
        segments = tuple(segments)
        if media_id is not None and self._checkpoint is not None:
            cached = await self._checkpoint.load_chunks(media_id)
            if cached is not None and chunks_compatible(cached, segments):
                self._increment("chunkCheckpointHits")
                return tuple(cached)
            if cached:
                self._increment("chunkCheckpointInvalidations")

        chunks = tuple(await self._chunking.build(segments))
        if media_id is not None:
            if self._checkpoint is not None:
                await self._checkpoint.save_chunks(media_id, chunks)
            # Retrieval indexing is intentionally best effort inside its own
            # service; a successful chunk build should still return here.
            await self._retrieval.index(media_id, chunks)
        return chunks

    def _increment(self, metric: str, amount: int = 1) -> None:
        if self._telemetry is not None:
            self._telemetry.increment(metric, amount)

    def _observe(self, metric: str, value: float) -> None:
        if self._telemetry is not None:
            self._telemetry.observe(metric, value)

    # Java migration spellings.  The Python-native methods above are the
    # canonical API; these aliases do not alter argument or result semantics.
    selectRelevant = select_relevant
    searchEvidence = search_evidence
    refineForCritique = refine_for_critique


def _java_string_length(value: str) -> int:
    """Count Java UTF-16 code units rather than Python Unicode code points."""

    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


java_string_length = _java_string_length


def _near_segment(timestamp_ms: int, segment: VideoSegment) -> bool:
    margin = max(60_000, segment.end_ms - segment.start_ms)
    return (
        timestamp_ms >= max(0, segment.start_ms - margin)
        and timestamp_ms < segment.end_ms + margin
    )


def _segment_key(segment: VideoSegment) -> str:
    return f"{segment.start_ms}:{segment.end_ms}"


def _critique_query(goal: str, critique: CriticResult | None) -> str:
    if critique is None:
        return goal
    return "\n".join(
        (
            goal,
            " ".join(critique.feedback),
            " ".join(critique.missing_requirements),
            " ".join(critique.unsupported_claims),
        )
    )


critique_query = _critique_query
near_segment = _near_segment


__all__ = [
    "CHUNK_MILLISECONDS",
    "CHUNK_MS",
    "MAX_CONTEXT_CHARS",
    "LongVideoContextService",
    "critique_query",
    "java_string_length",
    "near_segment",
]

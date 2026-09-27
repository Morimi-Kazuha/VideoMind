"""Five-minute semantic chunk construction.

This module ports the policy in Java ``VideoChunkingService`` while keeping
the model and embedding calls behind the small application ports.  It is
deliberately free of provider, persistence, and transport imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from dovideo.domain import (
    CHUNKING_CONTRACT_VERSION,
    ChunkSummary,
    VideoChunk,
    VideoSegment,
    chunk_id_for,
)

from .errors import BudgetExceededError
from .ports.ai import ChunkSummaryPort, EmbeddingPort
from .ports.observability import TelemetryPort


CHUNK_MILLISECONDS = 5 * 60 * 1000
MAX_SUMMARY_FALLBACK_CHARS = 500


class VideoChunkingService:
    """Build immutable five-minute chunks from timestamped context segments.

    The Java implementation is synchronous because its provider utilities are
    synchronous.  The Python boundary is asynchronous so an LLM/embedding
    adapter can perform I/O without blocking the event loop.
    """

    def __init__(
        self,
        summary_port: ChunkSummaryPort,
        embedding_port: EmbeddingPort,
        telemetry: TelemetryPort | None = None,
    ) -> None:
        self._summary_port = summary_port
        self._embedding_port = embedding_port
        self._telemetry = telemetry

    async def build(
        self,
        segments: Iterable[VideoSegment | None] | None,
    ) -> tuple[VideoChunk, ...]:
        """Build chunks in ascending five-minute windows.

        ``None`` entries are ignored just as Java's stream filter ignores
        null segments.  Python's stable sort retains the original order of
        equal-start segments, which is observable in the raw segment list.
        """

        if segments is None:
            return ()
        ordered = tuple(
            sorted(
                (segment for segment in segments if segment is not None),
                key=lambda segment: segment.start_ms,
            )
        )
        if not ordered:
            return ()

        chunks: list[VideoChunk] = []
        last_start = ordered[-1].start_ms
        for chunk_start in range(0, last_start + 1, CHUNK_MILLISECONDS):
            chunk_end = chunk_start + CHUNK_MILLISECONDS
            raw_segments = tuple(
                segment
                for segment in ordered
                if chunk_start <= segment.start_ms < chunk_end
            )
            if not raw_segments:
                continue

            summary = await self._summarize(raw_segments)
            keywords = _normalize_terms(summary.keywords)
            summary_text = summary.segment_summary
            embedding_text = summary_text + "\n" + " ".join(keywords)
            embedding = await self._embed(embedding_text)
            source_revision = _common_source_revision(raw_segments)
            chunks.append(
                VideoChunk(
                    start_ms=chunk_start,
                    end_ms=chunk_end,
                    segment_summary=summary_text,
                    keywords=keywords,
                    raw_segments=raw_segments,
                    embedding=embedding,
                    chunk_id=chunk_id_for(
                        source_revision,
                        chunk_start,
                        chunk_end,
                    ),
                    source_revision=source_revision,
                    chunking_version=(
                        CHUNKING_CONTRACT_VERSION if source_revision else ""
                    ),
                )
            )
        return tuple(chunks)

    async def _summarize(
        self,
        segments: Sequence[VideoSegment],
    ) -> ChunkSummary:
        try:
            summary = await self._summary_port.summarize_chunk(segments)
            if not isinstance(summary, ChunkSummary):
                raise TypeError("chunk summary port returned an invalid value")
            return summary
        except BudgetExceededError:
            raise
        except Exception:
            self._increment("summaryFallbacks")
            return ChunkSummary(
                segment_summary=_fallback_summary(segments),
                keywords=(),
            )

    async def _embed(self, text: str) -> tuple[float, ...]:
        try:
            embedding = await self._embedding_port.embed(text)
            if embedding is None:
                raise TypeError("embedding port returned None")
            return tuple(embedding)
        except Exception:
            self._increment("embeddingFallbacks")
            return ()

    def _increment(self, metric: str, amount: int = 1) -> None:
        if self._telemetry is not None:
            self._telemetry.increment(metric, amount)


def _normalize_terms(values: Iterable[str | None] | None) -> tuple[str, ...]:
    """Trim, discard blanks, and distinct terms while preserving order."""

    if values is None:
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None or not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return tuple(output)


def _fallback_summary(segments: Sequence[VideoSegment]) -> str:
    """Create Java-compatible raw fallback text, bounded to 500 characters."""

    parts: list[str] = []
    for segment in segments:
        # Keep the Java shape (transcript + one space + OCR text) so the
        # fallback remains wire-compatible; VideoChunk trims its outer space.
        text = segment.transcript + " " + " ".join(
            _normalize_terms(segment.ocr_texts)
        )
        if text.strip():
            parts.append(text)
    return " ".join(parts)[:MAX_SUMMARY_FALLBACK_CHARS]


def _common_source_revision(segments: Sequence[VideoSegment]) -> str:
    """Return one revision only when every segment agrees on provenance."""

    revisions = {segment.source_revision for segment in segments if segment.source_revision}
    return next(iter(revisions)) if len(revisions) == 1 else ""


__all__ = [
    "CHUNK_MILLISECONDS",
    "MAX_SUMMARY_FALLBACK_CHARS",
    "VideoChunkingService",
]

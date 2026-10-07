"""Five-minute sliding retrieval windows over canonical video segments."""

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


CHUNK_WINDOW_MILLISECONDS = 5 * 60 * 1000
CHUNK_OVERLAP_MILLISECONDS = 60 * 1000
CHUNK_STRIDE_MILLISECONDS = CHUNK_WINDOW_MILLISECONDS - CHUNK_OVERLAP_MILLISECONDS
CHUNK_MILLISECONDS = CHUNK_WINDOW_MILLISECONDS
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
        """Build ascending windows with one-minute overlap.

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
        for chunk_start, chunk_end, raw_segments in chunk_windows(ordered):
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
                    chunking_version=CHUNKING_CONTRACT_VERSION,
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
        except BudgetExceededError:
            raise
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

    revisions = {segment.source_revision for segment in segments}
    return next(iter(revisions)) if len(revisions) == 1 else ""


def chunk_windows(segments: Sequence[VideoSegment]) -> tuple[tuple[int, int, tuple[VideoSegment, ...]], ...]:
    """Plan windows without provider calls, preserving whole intersecting segments.

    Windows live on the zero-anchored 4 minute grid. Empty gaps are skipped.
    Once the maximum segment end is covered, no additional tail is generated.
    A long noncanonical segment may intersect multiple windows; no input is cut.
    """
    ordered = tuple(sorted(segments, key=lambda segment: segment.start_ms))
    if not ordered:
        return ()
    tail = max(segment.end_ms for segment in ordered)
    windows = []
    start = 0
    while True:
        end = start + CHUNK_WINDOW_MILLISECONDS
        raw = tuple(s for s in ordered if s.start_ms < end and s.end_ms > start)
        if raw:
            windows.append((start, end, raw))
        if end >= tail:
            break
        start += CHUNK_STRIDE_MILLISECONDS
    return tuple(windows)


def chunks_compatible(chunks: Sequence[VideoChunk], segments: Sequence[VideoSegment]) -> bool:
    """Treat old, incomplete or foreign chunk payloads as a checkpoint miss."""
    windows = chunk_windows(segments)
    if not chunks or len(chunks) != len(windows):
        return False
    for chunk, (start, end, raw) in zip(chunks, windows, strict=True):
        revision = _common_source_revision(raw)
        if (
            chunk.chunking_version != CHUNKING_CONTRACT_VERSION
            or (chunk.start_ms, chunk.end_ms) != (start, end)
            or chunk.source_revision != revision
            or chunk.chunk_id != chunk_id_for(revision, start, end)
            or chunk.raw_segments != raw
        ):
            return False
    return True


__all__ = [
    "CHUNK_MILLISECONDS",
    "CHUNK_WINDOW_MILLISECONDS",
    "CHUNK_OVERLAP_MILLISECONDS",
    "CHUNK_STRIDE_MILLISECONDS",
    "chunk_windows",
    "chunks_compatible",
    "MAX_SUMMARY_FALLBACK_CHARS",
    "VideoChunkingService",
]

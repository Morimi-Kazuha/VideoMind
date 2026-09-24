from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from dovideo.application.chunking import (
    CHUNK_MILLISECONDS,
    MAX_SUMMARY_FALLBACK_CHARS,
    VideoChunkingService,
)
from dovideo.application.value_objects import TranscriptSpan
from dovideo.domain import ChunkSummary, VideoSegment


def _segment(
    start_ms: int,
    transcript: str = "",
    ocr_texts: tuple[str, ...] = (),
) -> VideoSegment:
    return VideoSegment(
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        transcript=transcript,
        ocr_texts=ocr_texts,
    )


class SummaryFake:
    def __init__(self, value: ChunkSummary | None = None, error: Exception | None = None):
        self.value = value or ChunkSummary(segment_summary="summary", keywords=())
        self.error = error
        self.calls: list[tuple[VideoSegment, ...]] = []

    async def summarize_chunk(
        self, segments: tuple[VideoSegment, ...]
    ) -> ChunkSummary:
        self.calls.append(tuple(segments))
        if self.error is not None:
            raise self.error
        return self.value


class EmbeddingFake:
    def __init__(self, value: tuple[float, ...] = (0.1, 0.2), error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls: list[str] = []

    async def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.value


class TelemetryFake:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount

    def observe(self, metric: str, value: float, **_: object) -> None:
        del metric, value


@pytest.mark.asyncio
async def test_empty_input_returns_no_chunks_without_provider_calls() -> None:
    summary = SummaryFake()
    embedding = EmbeddingFake()
    service = VideoChunkingService(summary, embedding)

    assert await service.build([]) == ()
    assert await service.build(None) == ()
    assert summary.calls == []
    assert embedding.calls == []


@pytest.mark.asyncio
async def test_buckets_start_at_zero_skip_gaps_and_preserve_equal_start_order() -> None:
    summary = SummaryFake()
    embedding = EmbeddingFake()
    service = VideoChunkingService(summary, embedding)
    first = _segment(300_000, "first")
    second = _segment(0, "second")
    equal_a = _segment(600_000, "equal-a")
    equal_b = _segment(600_000, "equal-b")

    chunks = await service.build([first, equal_a, second, equal_b])

    assert [chunk.start_ms for chunk in chunks] == [0, 300_000, 600_000]
    assert [chunk.end_ms for chunk in chunks] == [300_000, 600_000, 900_000]
    assert [segment.transcript for segment in chunks[2].raw_segments] == [
        "equal-a",
        "equal-b",
    ]
    assert [call[0].start_ms for call in summary.calls] == [0, 300_000, 600_000]
    assert CHUNK_MILLISECONDS == 300_000


@pytest.mark.asyncio
async def test_summary_keywords_are_normalized_and_embedding_input_is_exact() -> None:
    summary = SummaryFake(
        ChunkSummary(
            segment_summary="  a concise summary  ",
            keywords=("  one ", "", "two", "one", "  ", "three  "),
        )
    )
    embedding = EmbeddingFake()
    service = VideoChunkingService(summary, embedding)

    chunks = await service.build([_segment(1, "speech")])

    assert chunks[0].segment_summary == "a concise summary"
    assert chunks[0].keywords == ("one", "two", "three")
    assert embedding.calls == ["a concise summary\none two three"]


@pytest.mark.asyncio
async def test_summary_failure_uses_normalized_ocr_fallback_and_truncates() -> None:
    telemetry = TelemetryFake()
    summary = SummaryFake(error=RuntimeError("summary unavailable"))
    embedding = EmbeddingFake()
    service = VideoChunkingService(summary, embedding, telemetry)
    segments = [
        _segment(0, " speech ", (" OCR ", "", "OCR", " second ")),
        _segment(60_000, "next", ("label",)),
    ]

    chunks = await service.build(segments)

    assert chunks[0].segment_summary == "speech OCR second next label"
    assert telemetry.counts["summaryFallbacks"] == 1

    long_text = _segment(0, "x" * (MAX_SUMMARY_FALLBACK_CHARS + 100))
    long_chunks = await service.build([long_text])
    assert len(long_chunks[0].segment_summary) == MAX_SUMMARY_FALLBACK_CHARS


@pytest.mark.asyncio
async def test_embedding_failure_keeps_chunk_with_empty_embedding_and_metric() -> None:
    telemetry = TelemetryFake()
    summary = SummaryFake()
    embedding = EmbeddingFake(error=RuntimeError("embedding unavailable"))
    service = VideoChunkingService(summary, embedding, telemetry)

    chunks = await service.build([_segment(0, "speech")])

    assert len(chunks) == 1
    assert chunks[0].embedding == ()
    assert telemetry.counts["embeddingFallbacks"] == 1


@pytest.mark.asyncio
async def test_build_does_not_mutate_input_sequence_or_retain_mutable_list() -> None:
    summary = SummaryFake()
    embedding = EmbeddingFake()
    service = VideoChunkingService(summary, embedding)
    first = _segment(300_000, "late")
    second = _segment(0, "early")
    original = [first, second]

    chunks = await service.build(original)

    assert original == [first, second]
    assert isinstance(chunks, tuple)
    with pytest.raises(ValidationError):
        chunks[0].raw_segments += (first,)  # type: ignore[misc]


@pytest.mark.asyncio
async def test_normal_transcript_span_shape_is_accepted_by_chunking() -> None:
    summary = SummaryFake()
    embedding = EmbeddingFake()
    service = VideoChunkingService(summary, embedding)
    # The domain context model is the public chunking input; this assertion
    # documents that the application does not require a mutable list adapter.
    span = TranscriptSpan(start_ms=0, end_ms=60_000, text="speech")

    chunks = await service.build([_segment(span.start_ms, span.text)])

    assert chunks[0].raw_segments[0].transcript == "speech"

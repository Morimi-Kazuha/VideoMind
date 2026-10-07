from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application.long_context import (
    CHUNK_MILLISECONDS,
    MAX_CONTEXT_CHARS,
    LongVideoContextService,
    critique_query,
    java_string_length,
    near_segment,
)
from dovideo.domain import CriticResult, VideoChunk, VideoContext, VideoEvidenceHit, VideoSegment
from dovideo.application.chunking import chunk_windows
from dovideo.domain import CHUNKING_CONTRACT_VERSION


def _segment(
    start_ms: int,
    transcript: str = "",
    ocr_texts: tuple[str, ...] = (),
    *,
    end_ms: int | None = None,
) -> VideoSegment:
    return VideoSegment(
        start_ms=start_ms,
        end_ms=end_ms if end_ms is not None else start_ms + 60_000,
        transcript=transcript,
        ocr_texts=ocr_texts,
    )


def _chunk(start_ms: int, *segments: VideoSegment) -> VideoChunk:
    return VideoChunk(
        start_ms=start_ms,
        end_ms=start_ms + CHUNK_MILLISECONDS,
        raw_segments=segments,
    )


def _context(*segments: VideoSegment, goal: str = "goal") -> VideoContext:
    return VideoContext(source="video.mp4", user_goal=goal, segments=segments)


class ChunkerFake:
    def __init__(self, result: tuple[VideoChunk, ...] = ()) -> None:
        self.result = result
        self.calls: list[tuple[VideoSegment, ...]] = []

    async def build(self, segments: tuple[VideoSegment, ...]) -> tuple[VideoChunk, ...]:
        self.calls.append(tuple(segments))
        return self.result


class RetrievalFake:
    def __init__(
        self,
        selected: tuple[VideoSegment, ...] = (),
        hits: tuple[VideoEvidenceHit, ...] = (),
    ) -> None:
        self.selected = selected
        self.hits = hits
        self.retrieve_calls: list[tuple[int | None, str, tuple[VideoChunk, ...]]] = []
        self.search_calls: list[tuple[int | None, str, tuple[VideoChunk, ...]]] = []
        self.index_calls: list[tuple[int | None, tuple[VideoChunk, ...]]] = []

    async def retrieve(
        self,
        media_id: int | None,
        goal: str,
        chunks: tuple[VideoChunk, ...],
    ) -> tuple[VideoSegment, ...]:
        self.retrieve_calls.append((media_id, goal, tuple(chunks)))
        return self.selected

    async def search(
        self,
        media_id: int | None,
        query: str,
        chunks: tuple[VideoChunk, ...],
    ) -> tuple[VideoEvidenceHit, ...]:
        self.search_calls.append((media_id, query, tuple(chunks)))
        return self.hits

    async def index(
        self,
        media_id: int | None,
        chunks: tuple[VideoChunk, ...],
    ) -> None:
        self.index_calls.append((media_id, tuple(chunks)))


class CheckpointFake:
    def __init__(self, cached: tuple[VideoChunk, ...] | None = None) -> None:
        self.cached = cached
        self.load_calls: list[int] = []
        self.save_calls: list[tuple[int, tuple[VideoChunk, ...]]] = []

    async def load_chunks(self, media_id: int) -> tuple[VideoChunk, ...] | None:
        self.load_calls.append(media_id)
        return self.cached

    async def save_chunks(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        self.save_calls.append((media_id, tuple(chunks)))


class TelemetryFake:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.values: dict[str, list[float]] = {}

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount

    def observe(self, metric: str, value: float, **_: object) -> None:
        self.values.setdefault(metric, []).append(value)


def _service(
    chunker: ChunkerFake | None = None,
    retrieval: RetrievalFake | None = None,
    checkpoint: CheckpointFake | None = None,
    telemetry: TelemetryFake | None = None,
) -> tuple[LongVideoContextService, ChunkerFake, RetrievalFake, CheckpointFake, TelemetryFake]:
    chunker = chunker or ChunkerFake()
    retrieval = retrieval or RetrievalFake()
    checkpoint = checkpoint or CheckpointFake()
    telemetry = telemetry or TelemetryFake()
    return (
        LongVideoContextService(chunker, retrieval, checkpoint, telemetry),
        chunker,
        retrieval,
        checkpoint,
        telemetry,
    )


@pytest.mark.asyncio
async def test_empty_and_short_context_bypass_chunking_using_last_element() -> None:
    service, chunker, retrieval, checkpoint, telemetry = _service()
    empty = _context()
    # Deliberately out of timestamp order: Java checks the supplied last item,
    # not the maximum timestamp.
    long_first = _segment(600_000, "long")
    short_last = _segment(0, "short")
    context = _context(long_first, short_last)

    result_empty = await service.select_relevant(empty, media_id=7)
    result_short = await service.select_relevant(context, media_id=7)

    assert result_empty.segments == ()
    assert [segment.start_ms for segment in result_short.segments] == [0, 600_000]
    assert chunker.calls == []
    assert retrieval.retrieve_calls == []
    assert checkpoint.load_calls == []
    assert telemetry.counts["contextSegmentsDropped"] == 0
    assert CHUNK_MILLISECONDS == 300_000


@pytest.mark.asyncio
async def test_long_context_builds_retrieves_and_applies_budget() -> None:
    original = _segment(0, "original")
    selected = _segment(0, "selected")
    chunk = _chunk(0, original)
    chunker = ChunkerFake((chunk,))
    retrieval = RetrievalFake((selected,))
    service, chunker, retrieval, checkpoint, _ = _service(chunker, retrieval)
    context = _context(_segment(0, "early"), _segment(300_000, "last"))

    result = await service.select_relevant(context, media_id=None)

    assert result.segments == (selected,)
    assert chunker.calls == [context.segments]
    assert retrieval.retrieve_calls == [(None, "goal", (chunk,))]
    assert checkpoint.load_calls == []
    assert retrieval.index_calls == []


@pytest.mark.asyncio
async def test_checkpoint_hit_reuses_chunks_without_rebuild_or_index() -> None:
    context = _context(_segment(0, "early"), _segment(300_000, "last"))
    cached = tuple(VideoChunk(start_ms=start, end_ms=end, raw_segments=raw,
                             chunking_version=CHUNKING_CONTRACT_VERSION)
                   for start, end, raw in chunk_windows(context.segments))
    chunker = ChunkerFake((_chunk(0, _segment(0, "rebuilt")),))
    retrieval = RetrievalFake((_segment(0, "selected"),))
    checkpoint = CheckpointFake(cached)
    service, chunker, retrieval, checkpoint, telemetry = _service(
        chunker, retrieval, checkpoint
    )

    result = await service.select_relevant(
        context,
        media_id=42,
    )

    assert result.segments[0].transcript == "selected"
    assert checkpoint.load_calls == [42]
    assert chunker.calls == []
    assert retrieval.retrieve_calls == [(42, "goal", cached)]
    assert retrieval.index_calls == []
    assert telemetry.counts["chunkCheckpointHits"] == 1


@pytest.mark.asyncio
async def test_checkpoint_miss_saves_then_indexes_and_media_none_skips_checkpoint() -> None:
    rebuilt = (_chunk(0, _segment(0, "rebuilt")),)
    chunker = ChunkerFake(rebuilt)
    retrieval = RetrievalFake((_segment(0, "selected"),))
    checkpoint = CheckpointFake(None)
    service, chunker, retrieval, checkpoint, _ = _service(
        chunker, retrieval, checkpoint
    )
    long_context = _context(_segment(0, "early"), _segment(300_000, "last"))

    await service.select_relevant(long_context, media_id=9)
    assert checkpoint.load_calls == [9]
    assert checkpoint.save_calls == [(9, rebuilt)]
    assert retrieval.index_calls == [(9, rebuilt)]

    checkpoint.load_calls.clear()
    checkpoint.save_calls.clear()
    retrieval.index_calls.clear()
    await service.select_relevant(long_context, media_id=None)
    assert checkpoint.load_calls == []
    assert checkpoint.save_calls == []
    assert retrieval.index_calls == []


def test_budget_uses_utf16_counts_first_overflow_and_continue_then_sorts() -> None:
    telemetry = TelemetryFake()
    service, *_ = _service(telemetry=telemetry)
    first_candidate = _segment(300_000, "x" * 100)
    overflow = _segment(0, "y" * MAX_CONTEXT_CHARS)
    zero_cost = _segment(600_000, "", ("",))
    later_small = _segment(900_000, "z")
    context = _context(first_candidate, overflow, zero_cost, later_small)

    result = service.within_budget(
        context, (first_candidate, overflow, zero_cost, later_small)
    )

    assert result.segments == (first_candidate, zero_cost, later_small)
    assert telemetry.counts["contextSegmentsDropped"] == 1
    assert telemetry.values["contextChars"] == [101]

    first_oversize = _segment(300_000, "x" * (MAX_CONTEXT_CHARS + 1))
    oversize_result = service.within_budget(context, (first_oversize, overflow))
    assert oversize_result.segments == (first_oversize,)
    assert telemetry.values["contextChars"][-1] == MAX_CONTEXT_CHARS + 1

    astral = _segment(0, "😀" * (MAX_CONTEXT_CHARS // 2))
    after_astral = _segment(60_000, "x")
    astral_result = service.within_budget(context, (astral, after_astral))
    assert astral_result.segments == (astral,)
    assert java_string_length("😀") == 2
    assert telemetry.values["contextChars"][-1] == MAX_CONTEXT_CHARS


@pytest.mark.asyncio
async def test_search_evidence_empty_is_noop_and_nonempty_resolves_chunks() -> None:
    hit = VideoEvidenceHit(start_ms=0, end_ms=60_000, source="ASR", snippet="x")
    chunk = _chunk(0, _segment(0, "chunk"))
    chunker = ChunkerFake((chunk,))
    retrieval = RetrievalFake(hits=(hit,))
    service, chunker, retrieval, checkpoint, _ = _service(chunker, retrieval)

    assert await service.search_evidence(3, _context()) == ()
    context = _context(_segment(0, "early"), _segment(300_000, "last"))
    result = await service.search_evidence(3, context)

    assert result == (hit,)
    assert checkpoint.load_calls == [3]
    assert chunker.calls == [context.segments]
    assert retrieval.search_calls == [(3, "goal", (chunk,))]


@pytest.mark.asyncio
async def test_search_evidence_accepts_recovered_chunks_without_rebuilding_or_saving() -> None:
    hit = VideoEvidenceHit(start_ms=0, end_ms=60_000, source="ASR", snippet="cached")
    cached = (_chunk(0, _segment(0, "durable chunk")),)
    chunker = ChunkerFake((_chunk(0, _segment(0, "rebuilt")),))
    retrieval = RetrievalFake(hits=(hit,))
    checkpoint = CheckpointFake(None)
    service, chunker, retrieval, checkpoint, _ = _service(
        chunker,
        retrieval,
        checkpoint,
    )

    result = await service.search_evidence(
        42,
        _context(_segment(0, "full context")),
        chunks=cached,
    )

    assert result == (hit,)
    assert retrieval.search_calls == [(42, "goal", cached)]
    assert chunker.calls == []
    assert checkpoint.load_calls == []
    assert checkpoint.save_calls == []
    assert retrieval.index_calls == []


@pytest.mark.asyncio
async def test_refine_matches_margin_boundaries_query_and_ordered_dedupe() -> None:
    s0 = _segment(0, "zero")
    s1_old = _segment(300_000, "old")
    s1_new = _segment(300_000, "new")
    retry = _segment(600_000, "retry")
    prior = _segment(900_000, "prior")
    chunker = ChunkerFake((_chunk(0, s0, s1_new),))
    retrieval = RetrievalFake((retry,))
    service, _, retrieval, _, _ = _service(chunker, retrieval)
    full = _context(s1_old, s0, s1_new, goal=" original ")
    selected = _context(s1_old, prior)
    critique = CriticResult(
        feedback=("feedback",),
        missing_requirements=("missing",),
        unsupported_claims=("unsupported",),
        required_timestamps=(0, 119_999, 120_000, 240_000, 419_999, 420_000),
    )

    result = await service.refine_for_critique(5, full, selected, critique)

    # s0 includes 0 and 119999 but excludes 120000; s1 includes 240000 and
    # 419999 but excludes 420000.  The later duplicate replaces old while
    # retaining the first key's insertion slot, then retry/prior are appended.
    assert [segment.transcript for segment in result.segments] == [
        "zero",
        "new",
        "retry",
        "prior",
    ]
    assert retrieval.retrieve_calls[0][0] == 5
    assert retrieval.retrieve_calls[0][1] == (
        "original\nfeedback\nmissing\nunsupported"
    )
    assert result.user_goal == "original"
    assert full.segments == (s1_old, s0, s1_new)
    assert selected.segments == (s1_old, prior)
    assert near_segment(420_000, s1_new) is False
    assert near_segment(419_999, s1_new) is True


@pytest.mark.asyncio
async def test_refine_none_critique_uses_original_goal_query() -> None:
    retry = _segment(600_000, "retry")
    chunker = ChunkerFake((_chunk(0, retry),))
    retrieval = RetrievalFake((retry,))
    service, _, retrieval, _, _ = _service(chunker, retrieval)
    full = _context(_segment(0, "early"), _segment(300_000, "last"), goal="goal")
    selected = _context()

    result = await service.refine_for_critique(None, full, selected, None)

    assert retrieval.retrieve_calls[0][1] == "goal"
    assert result.user_goal == "goal"
    assert result.segments == (retry,)
    assert critique_query("goal", None) == "goal"

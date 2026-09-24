from __future__ import annotations

import asyncio
from collections import Counter
from math import isclose

import pytest

from dovideo.application.retrieval import (
    MAX_SNIPPET_LENGTH,
    MAX_USER_HITS,
    TOP_K,
    VECTOR_LOOKUP_LIMIT,
    VideoEvidenceRetrievalService,
    cosine_similarity,
    fallback_terms,
)
from dovideo.application.value_objects import VectorHit
from dovideo.domain import VideoChunk, VideoRetrievalIntent, VideoSegment


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


def _chunk(
    start_ms: int,
    *segments: VideoSegment,
    embedding: tuple[float, ...] = (),
    summary: str = "",
    keywords: tuple[str, ...] = (),
) -> VideoChunk:
    return VideoChunk(
        start_ms=start_ms,
        end_ms=start_ms + 300_000,
        segment_summary=summary,
        keywords=keywords,
        raw_segments=segments,
        embedding=embedding,
    )


class PlannerFake:
    def __init__(self, value: VideoRetrievalIntent | None = None, error: Exception | None = None):
        self.value = value or VideoRetrievalIntent(
            semantic_query="semantic query",
            keywords=(),
            visual_keywords=(),
        )
        self.error = error
        self.calls: list[str] = []

    async def plan_retrieval(self, goal: str) -> VideoRetrievalIntent:
        self.calls.append(goal)
        if self.error is not None:
            raise self.error
        return self.value


class EmbeddingFake:
    def __init__(self, value: tuple[float, ...] = (1.0, 0.0), error: Exception | None = None):
        self.value = value
        self.error = error
        self.calls: list[str] = []

    async def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.value


class VectorFake:
    def __init__(self, hits: tuple[VectorHit, ...] = (), error: Exception | None = None):
        self.hits = hits
        self.error = error
        self.search_calls: list[tuple[int | None, tuple[float, ...], int]] = []
        self.upsert_calls: list[tuple[int | None, tuple[VideoChunk, ...]]] = []

    async def search(
        self,
        media_id: int,
        query_embedding: tuple[float, ...],
        *,
        limit: int,
    ) -> tuple[VectorHit, ...]:
        self.search_calls.append((media_id, query_embedding, limit))
        if self.error is not None:
            raise self.error
        return self.hits

    async def upsert(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        if self.error is not None:
            raise self.error
        self.upsert_calls.append((media_id, chunks))

    async def delete_media(self, media_id: int) -> None:
        del media_id


class TelemetryFake:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.values: dict[str, list[float]] = {}

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount

    def observe(self, metric: str, value: float, **_: object) -> None:
        self.values.setdefault(metric, []).append(value)


def _service(
    *,
    planner: PlannerFake | None = None,
    embedding: EmbeddingFake | None = None,
    vector: VectorFake | None = None,
    telemetry: TelemetryFake | None = None,
) -> tuple[VideoEvidenceRetrievalService, PlannerFake, EmbeddingFake, VectorFake, TelemetryFake]:
    planner = planner or PlannerFake()
    embedding = embedding or EmbeddingFake()
    vector = vector or VectorFake()
    telemetry = telemetry or TelemetryFake()
    return (
        VideoEvidenceRetrievalService(planner, embedding, vector, telemetry),
        planner,
        embedding,
        vector,
        telemetry,
    )


@pytest.mark.asyncio
async def test_planner_success_is_used_and_embedding_receives_semantic_query() -> None:
    planner = PlannerFake(
        VideoRetrievalIntent(
            semantic_query="  semantic  ", keywords=("term",), visual_keywords=("画面",)
        )
    )
    service, planner, embedding, _, telemetry = _service(planner=planner)

    await service.retrieve(7, "goal", [_chunk(0, _segment(0, "term"))])

    assert planner.calls == ["goal"]
    assert embedding.calls == ["semantic"]
    assert telemetry.counts["retrievalIntentFallbacks"] == 0


@pytest.mark.asyncio
async def test_blank_planner_intent_uses_fallback_terms_without_exception_metric() -> None:
    planner = PlannerFake(
        VideoRetrievalIntent(semantic_query=" ", keywords=("ignored",), visual_keywords=())
    )
    service, _, _, _, telemetry = _service(planner=planner)

    intent = await service._retrieval_intent(" Alpha，Beta! x")

    assert intent.semantic_query == "Alpha，Beta! x"
    assert intent.keywords == ("Alpha", "Beta")
    assert intent.visual_keywords == ("Alpha", "Beta")
    assert telemetry.counts["retrievalIntentFallbacks"] == 0


@pytest.mark.asyncio
async def test_planner_exception_uses_fallback_and_increments_metric() -> None:
    planner = PlannerFake(error=RuntimeError("planner down"))
    service, _, _, _, telemetry = _service(planner=planner)

    intent = await service._retrieval_intent("short")

    assert intent.keywords == ("short",)
    assert telemetry.counts["retrievalIntentFallbacks"] == 1


def test_fallback_terms_match_punctuation_unicode_and_limits() -> None:
    assert fallback_terms("甲乙，丙丁。甲乙 / ignored") == (
        "甲乙",
        "丙丁",
        "ignored",
    )
    assert fallback_terms("a ! b") == ("a ! b",)
    assert fallback_terms("  \u3000 ") == ()
    assert fallback_terms(None) == ()


@pytest.mark.asyncio
async def test_remote_vector_scores_override_local_cosine_and_use_six_limit() -> None:
    chunks = [
        _chunk(0, _segment(0, "local low"), embedding=(0.0, 1.0)),
        _chunk(300_000, _segment(300_000, "remote high"), embedding=(0.0, 1.0)),
    ]
    vector = VectorFake(hits=(VectorHit(300_000, 600_000, 1.0),))
    service, _, embedding, vector, _ = _service(vector=vector)

    result = await service.retrieve(99, "goal", chunks)

    assert result[0].transcript == "remote high"
    assert embedding.calls == ["semantic query"]
    assert vector.search_calls == [(99, (1.0, 0.0), VECTOR_LOOKUP_LIMIT)]
    assert VECTOR_LOOKUP_LIMIT == TOP_K * 2 == 6


@pytest.mark.asyncio
async def test_vector_failure_falls_back_to_local_cosine_and_metric() -> None:
    planner = PlannerFake(
        VideoRetrievalIntent(semantic_query="q", keywords=(), visual_keywords=())
    )
    service, _, _, _, telemetry = _service(
        planner=planner,
        embedding=EmbeddingFake((1.0, 0.0)),
        vector=VectorFake(error=RuntimeError("index unavailable")),
    )
    chunks = [
        _chunk(0, _segment(0, "cosine high"), embedding=(1.0, 0.0)),
        _chunk(300_000, _segment(300_000, "cosine low"), embedding=(0.0, 1.0)),
    ]

    result = await service.retrieve(1, "q", chunks)

    assert result[0].transcript == "cosine high"
    assert telemetry.counts["vectorStoreFallbacks"] == 1


@pytest.mark.asyncio
async def test_embedding_failure_uses_lexical_fallback_and_metric() -> None:
    planner = PlannerFake(
        VideoRetrievalIntent(semantic_query="q", keywords=("needle",), visual_keywords=())
    )
    service, _, _, _, telemetry = _service(
        planner=planner,
        embedding=EmbeddingFake(error=RuntimeError("embedding down")),
    )
    chunks = [
        _chunk(0, _segment(0, "needle"), summary="needle summary"),
        _chunk(300_000, _segment(300_000, "other")),
    ]

    result = await service.retrieve(None, "q", chunks)

    assert result[0].transcript == "needle"
    assert telemetry.counts["embeddingFallbacks"] == 1


@pytest.mark.asyncio
async def test_keyword_and_ocr_signals_rank_visual_match() -> None:
    planner = PlannerFake(
        VideoRetrievalIntent(
            semantic_query="q", keywords=(), visual_keywords=("logo",)
        )
    )
    service, *_ = _service(
        planner=planner,
        embedding=EmbeddingFake(error=RuntimeError("no local vector")),
    )
    chunks = [
        _chunk(0, _segment(0, "unrelated", ("logo",))),
        _chunk(300_000, _segment(300_000, "speech")),
    ]

    result = await service.retrieve(None, "q", chunks)

    # Both chunk and segment scoring include the OCR/transcript terms; the
    # visual-only segment remains a valid and searchable result.
    assert result[0].ocr_texts == ("logo",)


def test_cosine_handles_mismatch_empty_and_zero_vectors() -> None:
    assert cosine_similarity((1.0,), (1.0, 0.0)) == 0.0
    assert cosine_similarity((), ()) == 0.0
    assert cosine_similarity((0.0, 0.0), (1.0, 2.0)) == 0.0
    assert isclose(cosine_similarity((1.0, 0.0), (1.0, 0.0)), 1.0)


@pytest.mark.asyncio
async def test_exact_chunk_weights_top_three_and_segment_sorting() -> None:
    planner = PlannerFake(
        VideoRetrievalIntent(
            semantic_query="q", keywords=("speech",), visual_keywords=("logo",)
        )
    )
    service, _, _, _, telemetry = _service(
        planner=planner,
        embedding=EmbeddingFake(error=RuntimeError("force local zero")),
    )
    chunks = [
        _chunk(0, _segment(20, "speech", ("logo",))),
        _chunk(300_000, _segment(10, "speech")),
        _chunk(600_000, _segment(30, "")),
        _chunk(900_000, _segment(40, "ignored")),
    ]

    result = await service.retrieve(None, "q", chunks)

    # The first three chunks are considered; the segment with both signals is
    # ranked first despite its later timestamp.
    assert [item.start_ms for item in result] == [20, 10, 30]
    assert telemetry.counts["retrievalChunks"] == TOP_K

    intent = VideoRetrievalIntent(
        semantic_query="q", keywords=("speech",), visual_keywords=("logo",)
    )
    scored = service._score(intent, (), {}, chunks[0])
    # semantic=0, keyword=1, visual=1 -> .25 + .15.
    assert isclose(scored, 0.40)


@pytest.mark.asyncio
async def test_retrieve_returns_all_segments_from_top_three_and_preserves_input() -> None:
    service, *_ = _service()
    chunks = [
        _chunk(600_000, _segment(600_000, "third")),
        _chunk(0, _segment(0, "first-a"), _segment(1, "first-b")),
        _chunk(300_000, _segment(300_000, "second")),
        _chunk(900_000, _segment(900_000, "not selected")),
    ]
    original = list(chunks)

    result = await service.retrieve(None, "goal", chunks)

    assert len(result) == 4
    assert chunks == original


@pytest.mark.asyncio
async def test_search_caps_eight_hits_source_labels_and_snippet_shape() -> None:
    long_text = "  " + ("long\ntext " * 40)
    segments = (
        _segment(0, "asr"),
        _segment(60_000, "", ("ocr", "ocr", " ")),
        _segment(120_000, "both", ("画面", "画面")),
        _segment(180_000),
        _segment(240_000, long_text),
        _segment(300_000, "six"),
        _segment(360_000, "seven"),
        _segment(420_000, "eight"),
        _segment(480_000, "nine"),
    )
    service, *_ = _service()

    hits = await service.search(None, "goal", [_chunk(0, *segments)])

    assert len(hits) == MAX_USER_HITS == 8
    assert hits[0].source == "ASR"
    assert hits[1].source == "OCR"
    assert hits[2].source == "ASR+OCR"
    assert hits[3].source == "时间片段"
    assert len(hits[4].snippet) == MAX_SNIPPET_LENGTH + 3
    assert hits[4].snippet.endswith("...")
    assert hits[1].ocr_texts == ("ocr",)


@pytest.mark.asyncio
async def test_index_success_and_failure_metrics() -> None:
    chunks = [_chunk(0, _segment(0, "a"))]
    service, _, _, vector, telemetry = _service()

    await service.index(3, chunks)

    assert vector.upsert_calls == [(3, tuple(chunks))]
    assert telemetry.counts["vectorStoreWrites"] == 1

    failing_service, _, _, _, failing_telemetry = _service(
        vector=VectorFake(error=RuntimeError("write down"))
    )
    await failing_service.index(3, chunks)
    assert failing_telemetry.counts["vectorStoreFallbacks"] == 1


@pytest.mark.asyncio
async def test_index_cancellation_is_not_converted_to_vector_fallback() -> None:
    class CancelVector(VectorFake):
        async def upsert(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
            del media_id, chunks
            raise asyncio.CancelledError

    service, _, _, _, telemetry = _service(vector=CancelVector())

    with pytest.raises(asyncio.CancelledError):
        await service.index(1, [_chunk(0, _segment(0))])
    assert telemetry.counts["vectorStoreFallbacks"] == 0

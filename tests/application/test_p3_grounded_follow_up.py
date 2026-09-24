from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from dovideo.application import (
    FollowUpFailure,
    FollowUpModelFailure,
    GroundedFollowUpService,
    TaskKey,
)
from dovideo.domain import (
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    GroundedFollowUpAnswer,
    GroundedFollowUpEvidence,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
    VideoSegment,
)

_UNSET = object()


def _context() -> VideoContext:
    return VideoContext(
        source="minio://media/video-42.mp4",
        user_goal="",
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=10_000,
                transcript="算法的时间复杂度是 O(n)，每个元素只处理一次。",
                ocr_texts=("复杂度 O(n)",),
            ),
            VideoSegment(
                start_ms=10_000,
                end_ms=20_000,
                transcript="空间复杂度是 O(h)，由递归栈高度决定。",
                ocr_texts=("空间复杂度 O(h)",),
            ),
        ),
    )


def _chunks() -> tuple[VideoChunk, ...]:
    context = _context()
    return (
        VideoChunk(
            start_ms=0,
            end_ms=300_000,
            segment_summary="复杂度说明",
            keywords=("算法", "复杂度"),
            raw_segments=context.segments,
            embedding=(0.1, 0.2),
        ),
    )


def _hits() -> tuple[VideoEvidenceHit, ...]:
    return (
        VideoEvidenceHit(
            start_ms=0,
            end_ms=10_000,
            source="ASR+OCR",
            snippet="算法的时间复杂度是 O(n)",
            transcript="算法的时间复杂度是 O(n)，每个元素只处理一次。",
            ocr_texts=("复杂度 O(n)",),
        ),
        VideoEvidenceHit(
            start_ms=10_000,
            end_ms=20_000,
            source="ASR+OCR",
            snippet="空间复杂度 O(h)",
            transcript="空间复杂度是 O(h)，由递归栈高度决定。",
            ocr_texts=("空间复杂度 O(h)",),
        ),
    )


def _answer(
    *,
    candidate_index: int = 0,
    timestamp_ms: int = 1_200,
    source: str = "ASR",
    content: str = "算法的时间复杂度是 O(n)",
    claim: str = "算法的时间复杂度是 O(n)",
    answer: str = "算法的时间复杂度是 O(n)。",
) -> GroundedFollowUpAnswer:
    return GroundedFollowUpAnswer(
        answer=answer,
        evidence=(
            GroundedFollowUpEvidence(
                candidate_index=candidate_index,
                timestamp_ms=timestamp_ms,
                source=source,
                content=content,
                claim=claim,
            ),
        ),
    )


class _Checkpoint:
    def __init__(
        self,
        *,
        context: VideoContext | None | object = _UNSET,
        chunks: tuple[VideoChunk, ...] | None | object = _UNSET,
        result: AgentState | None = None,
    ) -> None:
        self.context = _context() if context is _UNSET else context
        self.chunks = _chunks() if chunks is _UNSET else chunks
        self.result = result
        self.context_reads = 0
        self.chunk_reads = 0
        self.result_keys: list[TaskKey] = []
        self.writes: list[str] = []

    async def load_context(self, media_id: int):
        self.context_reads += 1
        return self.context

    async def load_chunks(self, media_id: int):
        self.chunk_reads += 1
        return self.chunks

    async def load_result(self, key: TaskKey):
        self.result_keys.append(key)
        return self.result

    async def save_context(self, *_args, **_kwargs):
        self.writes.append("context")

    async def save_chunks(self, *_args, **_kwargs):
        self.writes.append("chunks")

    async def save_result(self, *_args, **_kwargs):
        self.writes.append("result")


class _Retrieval:
    def __init__(self, hits: Sequence[VideoEvidenceHit] | Exception | None = None) -> None:
        self.hits = _hits() if hits is None else hits
        self.calls: list[tuple[int | None, VideoContext, tuple[VideoChunk, ...]]] = []

    async def search_evidence(self, media_id, context, *, chunks=None):
        self.calls.append((media_id, context, tuple(chunks or ())))
        if isinstance(self.hits, Exception):
            raise self.hits
        return tuple(self.hits)


class _Model:
    def __init__(self, response: object | Exception | None = None) -> None:
        self.response = _answer() if response is None else response
        self.calls: list[dict[str, object]] = []

    async def answer(
        self,
        question: str,
        *,
        original_goal: str,
        profile,
        prior_analysis: Mapping[str, object] | None,
        sources: Sequence[VideoEvidenceHit],
    ):
        self.calls.append(
            {
                "question": question,
                "goal": original_goal,
                "profile": profile,
                "prior": prior_analysis,
                "sources": tuple(sources),
            }
        )
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _Observer:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record_follow_up_event(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _service(checkpoint=None, retrieval=None, model=None, observer=None):
    checkpoint = checkpoint or _Checkpoint()
    retrieval = retrieval or _Retrieval()
    model = model or _Model()
    service = GroundedFollowUpService(
        checkpoint,
        retrieval,
        model,
        observer=observer,
    )
    return service, checkpoint, retrieval, model


@pytest.mark.parametrize("mode", tuple(AnalysisMode))
@pytest.mark.asyncio
async def test_follow_up_reuses_context_chunks_retrieval_and_concrete_mode(mode) -> None:
    goal = "理解并整理视频中的算法复杂度"
    state = AgentState(
        goal=goal,
        result=AnalysisResult(
            title="复杂度分析",
            conclusions=("算法的时间复杂度是 O(n)",),
            evidence=(
                AnalysisEvidence(
                    timestamp_ms=1_200,
                    source="ASR",
                    content="算法的时间复杂度是 O(n)",
                    claim="算法的时间复杂度是 O(n)",
                ),
            ),
        ),
    )
    observer = _Observer()
    service, checkpoint, retrieval, model = _service(
        checkpoint=_Checkpoint(result=state),
        observer=observer,
    )

    result = await service.answer(42, "为什么是线性复杂度？", goal, mode)

    assert "[00:00–00:10] ASR" in result
    assert "算法的时间复杂度是 O(n)" in result
    assert checkpoint.context_reads == 1
    assert checkpoint.chunk_reads == 1
    assert checkpoint.result_keys == [TaskKey(42, goal, mode)]
    assert checkpoint.writes == []
    assert len(retrieval.calls) == 1
    media_id, query_context, chunks = retrieval.calls[0]
    assert media_id == 42
    assert query_context.user_goal == "为什么是线性复杂度？"
    assert query_context.segments == checkpoint.context.segments
    assert chunks == checkpoint.chunks
    assert model.calls[0]["goal"] == goal
    assert model.calls[0]["profile"].mode is mode
    assert model.calls[0]["prior"]["title"] == "复杂度分析"
    supplied_sources = model.calls[0]["sources"]
    assert any(source.transcript for source in supplied_sources)
    assert any(source.ocr_texts for source in supplied_sources)
    assert "context_recovery_succeeded" in [event for event, _ in observer.events]
    assert "retrieval_completed" in [event for event, _ in observer.events]
    retrieval_metrics = next(
        fields
        for event, fields in observer.events
        if event == "retrieval_completed"
    )
    assert retrieval_metrics["asr_candidates"] >= 1
    assert retrieval_metrics["ocr_candidates"] >= 1
    assert "provider_call_succeeded" in [event for event, _ in observer.events]
    assert "evidence_verification_succeeded" in [
        event for event, _ in observer.events
    ]
    assert goal not in repr(observer.events)
    assert "为什么是线性复杂度" not in repr(observer.events)
    assert checkpoint.context.segments[0].transcript not in repr(observer.events)


@pytest.mark.asyncio
async def test_prior_analysis_context_is_structurally_bounded_and_excludes_evidence() -> None:
    goal = "原始目标"
    state = AgentState(
        goal=goal,
        result=AnalysisResult(
            title="标题" * 500,
            conclusions=tuple(f"结论-{index}-" + "x" * 500 for index in range(8)),
            suggestions=tuple(f"建议-{index}-" + "y" * 500 for index in range(8)),
            evidence=(
                AnalysisEvidence(
                    timestamp_ms=1_200,
                    source="ASR",
                    content="不应作为 source evidence 的长文本" * 100,
                    claim="不应当进入 prior analysis",
                ),
            ),
        ),
    )
    service, _, _, model = _service(checkpoint=_Checkpoint(result=state))

    await service.answer(42, "请解释结论", goal, AnalysisMode.LEARNING)

    prior = model.calls[0]["prior"]
    assert len(prior["title"]) <= 160
    assert len(prior["conclusions"]) == 4
    assert len(prior["suggestions"]) == 2
    assert all(len(item) <= 220 for item in prior["conclusions"])
    assert all(len(item) <= 160 for item in prior["suggestions"])
    assert "evidence" not in prior


@pytest.mark.parametrize(
    ("context", "chunks", "category"),
    [
        (None, _chunks(), "context_not_ready"),
        (_context(), None, "context_not_ready"),
    ],
)
@pytest.mark.asyncio
async def test_missing_durable_context_or_chunks_fails_readiness_without_retrieval(
    context,
    chunks,
    category,
) -> None:
    service, _, retrieval, model = _service(
        checkpoint=_Checkpoint(context=context, chunks=chunks)
    )

    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, "问题", "目标", AnalysisMode.GENERAL)

    assert error.value.category == category
    assert retrieval.calls == []
    assert model.calls == []


@pytest.mark.parametrize(
    "hits",
    [
        (),
        (
            VideoEvidenceHit(
                start_ms=0,
                end_ms=10_000,
                source="时间片段",
                snippet="no source text",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_no_usable_retrieval_evidence_does_not_call_model(hits) -> None:
    service, _, _, model = _service(retrieval=_Retrieval(hits))

    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, "问题", "目标", AnalysisMode.GENERAL)

    assert error.value.category == "no_evidence"
    assert model.calls == []


@pytest.mark.parametrize(
    "response",
    [
        _answer(timestamp_ms=20_000),
        _answer(source="OCR", content="不在 OCR 中的内容"),
        _answer(claim="没有出现在回答中的 claim"),
        _answer(candidate_index=7),
    ],
)
@pytest.mark.asyncio
async def test_bad_timestamp_source_quote_or_candidate_is_rejected(response) -> None:
    service, checkpoint, _, _ = _service(model=_Model(response))

    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, "问题", "目标", AnalysisMode.GENERAL)

    assert error.value.category == "evidence_rejected"
    assert checkpoint.writes == []


@pytest.mark.parametrize(
    ("source", "content"),
    [("OCR", "复杂度 O(n)"), ("ASR+OCR", "O(n)")],
)
@pytest.mark.asyncio
async def test_valid_ocr_and_combined_source_evidence_passes(source, content) -> None:
    response = _answer(
        source=source,
        content=content,
        claim=content,
        answer=f"视频片段显示{content}。",
    )
    service, checkpoint, _, _ = _service(model=_Model(response))

    result = await service.answer(42, "请核验画面文字", "解释算法", AnalysisMode.GENERAL)

    assert f"[00:00–00:10] {source}" in result
    assert content in result
    assert checkpoint.writes == []


@pytest.mark.asyncio
async def test_provider_failure_is_bounded_and_does_not_mutate_analysis_state() -> None:
    service, checkpoint, _, _ = _service(
        model=_Model(FollowUpModelFailure("timeout"))
    )

    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, "问题", "目标", AnalysisMode.REVIEW)

    assert error.value.category == "timeout"
    assert checkpoint.writes == []


@pytest.mark.asyncio
async def test_retrieval_failure_is_not_silently_converted_to_lexical_success() -> None:
    service, checkpoint, _, model = _service(
        retrieval=_Retrieval(RuntimeError("private transport detail"))
    )

    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, "问题", "目标", AnalysisMode.GENERAL)

    assert error.value.category == "retrieval_failure"
    assert "private" not in error.value.safe_message
    assert model.calls == []
    assert checkpoint.writes == []

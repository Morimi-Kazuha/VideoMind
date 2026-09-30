from __future__ import annotations

from collections.abc import Mapping, Sequence
import asyncio

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
    SourceItemIdentity,
    TemporalObservation,
    MAX_EVIDENCE_SOURCE_ITEM_REFS,
    PROVENANCE_VERSION,
    content_digest,
    source_item_id_for,
)
from dovideo.application.evidence import EvidenceVerificationService

_UNSET = object()


@pytest.mark.asyncio
async def test_r6_follow_up_deadline_cancels_stalled_checkpoint_without_trusted_writes():
    class Stalled(_Checkpoint):
        cancelled = False
        async def load_context(self, media_id):
            try:
                await asyncio.Future()
            finally:
                self.cancelled = True
    checkpoint = Stalled()
    service = GroundedFollowUpService(checkpoint, None, None, max_wall_seconds=0.001)
    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, 'question', 'goal', AnalysisMode.GENERAL)
    assert error.value.category == 'timeout'
    assert checkpoint.cancelled
    assert checkpoint.writes == []


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
        observations: Sequence[TemporalObservation] = (),
    ):
        self.calls.append(
            {
                "question": question,
                "goal": original_goal,
                "profile": profile,
                "prior": prior_analysis,
                "sources": tuple(sources),
                "observations": tuple(observations),
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


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['retrieval', 'model'])
async def test_r6_total_deadline_cancels_pending_downstream_boundary(monkeypatch, boundary):
    # Reschedule the real asyncio timeout when the boundary is reached; no sleep
    # or machine-speed assumption decides the order of this race.
    real_timeout = asyncio.timeout
    timeouts = []
    def controlled_timeout(delay):
        handle = real_timeout(delay)
        timeouts.append(handle)
        return handle
    monkeypatch.setattr(asyncio, 'timeout', controlled_timeout)
    cancelled = []
    async def stalled(*args, **kwargs):
        timeouts[0].reschedule(asyncio.get_running_loop().time())
        try:
            await asyncio.Future()
        finally:
            cancelled.append(boundary)
    checkpoint, retrieval, model = _Checkpoint(), _Retrieval(), _Model()
    if boundary == 'retrieval': retrieval.search_evidence = stalled
    else: model.answer = stalled
    service = GroundedFollowUpService(checkpoint, retrieval, model)
    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, 'question', 'goal', AnalysisMode.GENERAL)
    assert error.value.category == 'timeout'
    assert cancelled == [boundary]
    assert checkpoint.writes == []
    if boundary == 'retrieval': assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['checkpoint', 'retrieval', 'model'])
async def test_r6_native_deadline_error_keeps_timeout_classification(boundary):
    async def timed_out(*args, **kwargs): raise TimeoutError('private timeout')
    checkpoint, retrieval, model = _Checkpoint(), _Retrieval(), _Model()
    if boundary == 'checkpoint': checkpoint.load_context = timed_out
    elif boundary == 'retrieval': retrieval.search_evidence = timed_out
    else: model.answer = timed_out
    with pytest.raises(FollowUpFailure) as error:
        await GroundedFollowUpService(checkpoint, retrieval, model).answer(42, 'question', 'goal', AnalysisMode.GENERAL)
    assert error.value.category == 'timeout'
    assert 'private' not in error.value.safe_message
    assert checkpoint.writes == []


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


def _provenance_fixture(count, *, whole_quote=False, source="ASR"):
    revision, segment_id = "media-42-revision", "media-42-segment"
    texts = tuple(f"observation{index:02d}" for index in range(count))
    frame = "frame-42" if source == "OCR" else None
    items = tuple(SourceItemIdentity(
        source_item_id=source_item_id_for(
            revision, segment_id, source, index, 1200, 9000 if source == "ASR" else None,
            text, frame,
        ),
        source_revision=revision, segment_id=segment_id, source_type=source,
        ordinal=index, timestamp_ms=1200, end_ms=9000 if source == "ASR" else None,
        content_digest=content_digest(text),
        frame_ref_digest=content_digest(frame) if frame else "",
        provenance_version=PROVENANCE_VERSION,
    ) for index, text in enumerate(texts))
    segment = VideoSegment(
        start_ms=0, end_ms=10000, transcript="\n".join(texts) if source == "ASR" else "",
        ocr_texts=texts if source == "OCR" else (), evidence_frames=(frame,) if frame else (),
        source_revision=revision, segment_id=segment_id, source_items=items,
        provenance_version=PROVENANCE_VERSION,
    )
    context = VideoContext(
        source="minio://media/video-42.mp4", source_revision=revision,
        provenance_version=PROVENANCE_VERSION, segments=(segment,),
        observations=tuple(TemporalObservation(source_item=item, text=text, frame_ref=frame)
                           for item, text in zip(items, texts, strict=True)),
    )
    hit = VideoEvidenceHit(
        start_ms=0, end_ms=10000, transcript=segment.transcript, ocr_texts=segment.ocr_texts,
        source=source, snippet=texts[-1], source_revision=revision, segment_id=segment_id,
        source_item_ids=segment.source_item_ids,
    )
    quote = " ".join(texts) if whole_quote else texts[-1]
    response = _answer(source=source, content=quote, claim=quote, answer=quote)
    return context, hit, response


class _RecordingVerifier(EvidenceVerificationService):
    def __init__(self):
        self.checked = []

    def supported(self, context, evidence):
        self.checked.append(evidence)
        return super().supported(context, evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [12, 9, 8, 3])
async def test_r6_follow_up_resolves_quote_support_before_domain_reference_bound(count):
    context, hit, response = _provenance_fixture(count, whole_quote=count <= 8)
    checkpoint = _Checkpoint(context=context)
    verifier = _RecordingVerifier()
    service = GroundedFollowUpService(checkpoint, _Retrieval((hit,)), _Model(response), verifier=verifier)
    result = await service.answer(42, "question", "goal", AnalysisMode.GENERAL)
    evidence = verifier.checked[0]
    assert response.answer in result
    assert evidence.source_item_ids == (hit.source_item_ids if count <= 8 else hit.source_item_ids[-1:])
    assert 1 <= len(evidence.source_item_ids) <= MAX_EVIDENCE_SOURCE_ITEM_REFS
    assert set(evidence.source_item_ids) <= set(hit.source_item_ids)
    assert evidence.source_revision == context.source_revision
    assert evidence.segment_id == hit.segment_id
    assert evidence.timestamp_ms == 1200
    assert evidence.source_provenance_version == context.provenance_version
    assert verifier.provenance_supported(context, evidence)
    assert checkpoint.writes == []


@pytest.mark.asyncio
async def test_r6_duplicate_candidate_refs_are_stably_deduplicated():
    context, hit, response = _provenance_fixture(3, whole_quote=True)
    hit = hit.model_copy(update={"source_item_ids": tuple(item for item in hit.source_item_ids for _ in range(4))})
    verifier = _RecordingVerifier()
    service = GroundedFollowUpService(_Checkpoint(context=context), _Retrieval((hit,)), _Model(response), verifier=verifier)
    for _ in range(2):
        await service.answer(42, "question", "goal", AnalysisMode.GENERAL)
    assert all(e.source_item_ids == context.segments[0].source_item_ids for e in verifier.checked)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["foreign_media", "revision", "segment", "timestamp", "digest", "empty", "over_bound"])
async def test_r6_invalid_or_unbounded_support_rejects_without_fake_provenance(fault):
    context, hit, response = _provenance_fixture(9, whole_quote=fault == "over_bound")
    if fault == "foreign_media":
        hit = hit.model_copy(update={"source_item_ids": ("media-99-item",)})
    elif fault == "revision":
        hit = hit.model_copy(update={"source_revision": "old-revision"})
    elif fault == "segment":
        hit = hit.model_copy(update={"segment_id": "other-segment"})
    elif fault == "empty":
        hit = hit.model_copy(update={"source_item_ids": ()})
    elif fault in {"timestamp", "digest"}:
        segment = context.segments[0]
        changes = {"timestamp_ms": 2000} if fault == "timestamp" else {"content_digest": content_digest("unrelated")}
        segment = segment.model_copy(update={"source_items": tuple(item.model_copy(update=changes) for item in segment.source_items)})
        context = context.model_copy(update={"segments": (segment,)})
    observer = _Observer()
    checkpoint = _Checkpoint(context=context)
    service, _, _, _ = _service(checkpoint, _Retrieval((hit,)), _Model(response), observer)
    with pytest.raises(FollowUpFailure) as error:
        await service.answer(42, "question", "goal", AnalysisMode.GENERAL)
    assert error.value.category == "evidence_rejected"
    assert "evidence_verification_failed" in [event for event, _ in observer.events]
    assert "follow_up_succeeded" not in [event for event, _ in observer.events]
    assert checkpoint.writes == []


@pytest.mark.asyncio
async def test_r6_ocr_quote_preserves_original_frame_and_source_identity():
    context, hit, response = _provenance_fixture(2, source="OCR")
    before = context.model_dump_json()
    verifier = _RecordingVerifier()
    service = GroundedFollowUpService(_Checkpoint(context=context), _Retrieval((hit,)), _Model(response), verifier=verifier)
    await service.answer(42, "question", "goal", AnalysisMode.GENERAL)
    assert verifier.checked[0].source_item_ids == (context.observations[-1].source_item.source_item_id,)
    assert context.observations[-1].frame_ref == "frame-42"
    assert context.model_dump_json() == before


@pytest.mark.asyncio
async def test_r6_combined_quote_requires_supporting_refs_from_both_channels():
    context, hit, response = _provenance_fixture(3)
    segment = context.segments[0]
    quote = response.evidence[0].content
    ocr = SourceItemIdentity(
        source_item_id="media-42-ocr", source_revision=context.source_revision,
        segment_id=segment.segment_id, source_type="OCR", ordinal=0,
        timestamp_ms=1200, content_digest=content_digest(quote),
    )
    segment = segment.model_copy(update={"source_items": (*segment.source_items, ocr), "ocr_texts": (quote,)})
    context = context.model_copy(update={"segments": (segment,)})
    hit = hit.model_copy(update={"source_item_ids": segment.source_item_ids, "ocr_texts": (quote,)})
    response = _answer(source="ASR+OCR", content=quote, claim=quote, answer=quote)
    verifier = _RecordingVerifier()
    service = GroundedFollowUpService(_Checkpoint(context=context), _Retrieval((hit,)), _Model(response), verifier=verifier)
    await service.answer(42, "question", "goal", AnalysisMode.GENERAL)
    assert verifier.checked[0].source_item_ids == (segment.source_items[2].source_item_id, ocr.source_item_id)
    assert verifier.provenance_supported(context, verifier.checked[0])

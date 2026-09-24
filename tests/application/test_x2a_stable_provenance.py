from __future__ import annotations

import json
from typing import Any

import pytest

from dovideo.application import (
    AgentRole,
    AsrBranchOutcome,
    EvidenceVerificationService,
    MediaObservationBundle,
    ModelToolRequest,
    OcrBranchOutcome,
    TaskKey,
    ToolExecutionContext,
    ToolPolicyContext,
    ToolRegistry,
    VideoContextBuilder,
    VideoReadOnlyToolExecutor,
    VideoChunkingService,
    VideoEvidenceRetrievalService,
    chunk_id_for,
    compute_source_revision,
)
from dovideo.application.checkpoint_service import AgentCheckpointService
from dovideo.application.value_objects import OcrObservation, TranscriptSpan
from dovideo.domain import (
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    VideoChunk,
    VideoContext,
)
from dovideo.infrastructure import JsonCheckpointCodec, JsonHttpResponse, QdrantVectorIndex


def _observations(
    *,
    transcript: str = "alpha",
    ocr_text: str = "screen",
    frame_ref: str | None = "frame-1",
    timestamp_ms: int = 5,
) -> MediaObservationBundle:
    return MediaObservationBundle(
        asr=AsrBranchOutcome(
            observations=(TranscriptSpan(0, 100, transcript),),
            attempted=1,
        ),
        ocr=OcrBranchOutcome(
            observations=(OcrObservation(timestamp_ms, ocr_text, frame_ref),),
            attempted=1,
        ),
    )


def _context(**kwargs: Any) -> VideoContext:
    return VideoContextBuilder().build(
        "memory://video",
        "goal",
        _observations(**kwargs),
        media_content_identity="sha256:media-content",
    )


def test_source_revision_is_canonical_versioned_and_changes_with_authoritative_input() -> None:
    base = _observations()
    same = _observations()

    first = compute_source_revision(
        "sha256:media-content", base.asr.observations, base.ocr.observations
    )
    second = compute_source_revision(
        "sha256:media-content", same.asr.observations, same.ocr.observations
    )
    changed_text = compute_source_revision(
        "sha256:media-content",
        _observations(transcript="changed").asr.observations,
        base.ocr.observations,
    )
    changed_time = compute_source_revision(
        "sha256:media-content",
        base.asr.observations,
        _observations(timestamp_ms=6).ocr.observations,
    )
    changed_frame = compute_source_revision(
        "sha256:media-content",
        base.asr.observations,
        _observations(frame_ref="frame-2").ocr.observations,
    )

    assert first == second
    assert len(first) == 64
    assert first != changed_text
    assert first != changed_time
    assert first != changed_frame
    assert json.dumps({"b": 2, "a": 1}, sort_keys=True, separators=(",", ":")) == json.dumps(
        {"a": 1, "b": 2}, sort_keys=True, separators=(",", ":")
    )


def test_context_provenance_ids_are_stable_across_rebuild_and_revision_changes() -> None:
    first = _context()
    second = _context()
    changed = _context(transcript="changed")

    assert first.source_revision == second.source_revision
    assert first.segments[0].segment_id == second.segments[0].segment_id
    assert first.segments[0].source_item_ids == second.segments[0].source_item_ids
    assert first.source_revision != changed.source_revision
    assert first.segments[0].segment_id != changed.segments[0].segment_id
    assert first.segments[0].source_items[0].source_type == "ASR"
    assert first.segments[0].source_items[1].source_type == "OCR"


def test_checkpoint_round_trip_and_legacy_context_decode_are_compatible() -> None:
    context = _context()
    codec = JsonCheckpointCodec()
    restored = codec.decode(codec.encode(context), VideoContext)
    legacy = VideoContext.model_validate(
        {
            "source": "legacy",
            "userGoal": "goal",
            "segments": [
                {
                    "startMs": 0,
                    "endMs": 60_000,
                    "transcript": "legacy text",
                }
            ],
        }
    )

    assert restored == context
    assert restored.source_revision == context.source_revision
    assert legacy.source_revision == ""
    assert legacy.segments[0].source_items == ()


class _ContextCheckpointRepository:
    def __init__(self) -> None:
        self.value: Any | None = None

    def write(
        self,
        media_id: int,
        checkpoint: str,
        stage_checkpoint: str,
        redis_key: str,
        field: str,
        stage: Any,
        value: Any,
    ) -> None:
        self.value = value

    def read(
        self,
        media_id: int,
        checkpoint: str,
        redis_key: str,
        field: str,
        target_type: Any,
    ) -> Any | None:
        if self.value is None:
            return None
        return target_type.model_validate(
            self.value.model_dump(mode="python", by_alias=True)
        )


@pytest.mark.asyncio
async def test_context_checkpoint_service_preserves_provenance_identity() -> None:
    context = _context()
    service = AgentCheckpointService(_ContextCheckpointRepository())

    await service.save_context(7, context)
    restored = await service.load_context(7)

    assert restored is not None
    assert restored.source_revision == context.source_revision
    assert restored.provenance_version == context.provenance_version
    assert restored.segments[0].segment_id == context.segments[0].segment_id
    assert restored.segments[0].source_item_ids == context.segments[0].source_item_ids


def test_provenance_aware_evidence_guard_validates_identity_and_content() -> None:
    context = _context()
    segment = context.segments[0]
    asr_item, ocr_item = segment.source_items
    verifier = EvidenceVerificationService()

    valid = AnalysisEvidence(
        timestamp_ms=5,
        source="ASR+OCR",
        content="alpha screen",
        claim="claim",
        source_revision=context.source_revision,
        segment_id=segment.segment_id,
        source_item_ids=(asr_item.source_item_id, ocr_item.source_item_id),
        source_provenance_version=context.provenance_version,
    )
    assert verifier.supported(context, valid)

    assert not verifier.supported(
        context,
        valid.model_copy(update={"segment_id": "model-fabricated"}),
    )
    assert not verifier.supported(
        context,
        valid.model_copy(update={"source_revision": "0" * 64}),
    )
    assert not verifier.supported(
        context,
        valid.model_copy(update={"content": "not in either source item"}),
    )
    assert not verifier.supported(
        context,
        valid.model_copy(update={"source": "ASR", "source_item_ids": (ocr_item.source_item_id,)}),
    )
    assert not verifier.supported(
        context,
        valid.model_copy(update={"timestamp_ms": 10}),
    )


def test_legacy_evidence_path_remains_value_grounded_and_unambiguous_evidence_is_bound() -> None:
    context = _context()
    verifier = EvidenceVerificationService()
    legacy = AnalysisEvidence(
        timestamp_ms=5,
        source="ASR",
        content="alpha",
        claim="claim",
    )
    result = AnalysisResult(conclusions=("claim",), evidence=(legacy,))

    assert verifier.supported(context, legacy)
    bound = verifier.bind_provenance(context, result)
    assert bound is not None
    assert bound.evidence[0].source_revision == context.source_revision
    assert bound.evidence[0].segment_id == context.segments[0].segment_id
    assert bound.evidence[0].source_item_id == context.segments[0].source_items[0].source_item_id


def test_chunk_identity_is_revision_aware() -> None:
    context = _context()
    segment = context.segments[0]
    chunk = VideoChunk(
        start_ms=0,
        end_ms=300_000,
        raw_segments=(segment,),
        source_revision=context.source_revision,
        chunk_id=chunk_id_for(context.source_revision, 0, 300_000),
        chunking_version="video-chunk-5m-v1",
    )
    other = VideoChunk(
        start_ms=0,
        end_ms=300_000,
        raw_segments=(segment,),
        source_revision="1" * 64,
        chunk_id=chunk_id_for("1" * 64, 0, 300_000),
        chunking_version="video-chunk-5m-v1",
    )

    assert chunk.chunk_id == chunk_id_for(context.source_revision, 0, 300_000)
    assert chunk.chunk_id != other.chunk_id


@pytest.mark.asyncio
async def test_x1_video_tool_projection_exposes_bounded_provenance() -> None:
    context = _context()
    key = TaskKey(7, context.user_goal, AnalysisMode.GENERAL)
    policy_context = ToolPolicyContext(
        task_key=key,
        media_id=7,
        mode=AnalysisMode.GENERAL,
        agent_role=AgentRole.EXECUTOR,
        media_duration_ms=60_000,
    )
    execution_context = ToolExecutionContext.from_policy_context(
        policy_context,
        context,
    )
    call = ToolRegistry().create_call(
        ModelToolRequest(
            tool_name="video.get_segment",
            arguments={"timestamp_ms": 5},
        ),
        call_id="x2a-tool-call",
    )

    result = await VideoReadOnlyToolExecutor().execute(call, execution_context)
    segment = result.payload["segment"]
    assert segment["source_revision"] == context.source_revision
    assert segment["segment_id"] == context.segments[0].segment_id
    assert segment["source_item_ids"] == list(context.segments[0].source_item_ids)


class _QdrantClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, *, headers: dict[str, str], json: Any, timeout: float):
        self.calls.append({"method": method, "url": url, "json": json})
        if method == "GET":
            return JsonHttpResponse(
                200,
                {"result": {"config": {"params": {"vectors": {"size": 1}}}}},
            )
        return JsonHttpResponse(200, {})


@pytest.mark.asyncio
async def test_qdrant_new_points_carry_revision_and_chunk_identity() -> None:
    client = _QdrantClient()
    context = _context()
    chunk = VideoChunk(
        start_ms=0,
        end_ms=300_000,
        raw_segments=(context.segments[0],),
        embedding=(1.0,),
        source_revision=context.source_revision,
        chunk_id=chunk_id_for(context.source_revision, 0, 300_000),
        chunking_version="video-chunk-5m-v1",
    )
    await QdrantVectorIndex(client=client).upsert(7, (chunk,))
    payload = client.calls[-1]["json"]["points"][0]["payload"]
    assert payload["sourceRevision"] == context.source_revision
    assert payload["chunkId"] == chunk.chunk_id

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from dovideo.domain import (
    GroundedFollowUpAnswer,
    GroundedFollowUpEvidence,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
    VideoSegment,
    SourceItemIdentity,
    content_digest,
)
from dovideo.presentation.api.app import create_app
from dovideo.presentation.api.r4_runtime import ProductionR4Services
from dovideo.presentation.api.runtime import R1ServiceError


def _source_context() -> VideoContext:
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
        ),
    )


def _retrieved_hit() -> VideoEvidenceHit:
    return VideoEvidenceHit(
        start_ms=0,
        end_ms=10_000,
        source="ASR+OCR",
        snippet="算法的时间复杂度是 O(n)",
        transcript="算法的时间复杂度是 O(n)，每个元素只处理一次。",
        ocr_texts=("复杂度 O(n)",),
    )


def _answer_json() -> str:
    return GroundedFollowUpAnswer(
        answer="算法的时间复杂度是 O(n)。",
        evidence=(
            GroundedFollowUpEvidence(
                candidate_index=0,
                timestamp_ms=1_200,
                source="ASR",
                content="算法的时间复杂度是 O(n)",
                claim="算法的时间复杂度是 O(n)",
            ),
        ),
    ).model_dump_json(by_alias=True)


class _Auth:
    def require(self, authorization: str | None):
        if authorization == "Bearer 7":
            return {"id": 7}
        if authorization == "Bearer 8":
            return {"id": 8}
        raise R1ServiceError("请先登录", status_code=401)


class _Media:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def require_owned(self, media_id: int, user_id: int):
        self.calls.append((media_id, user_id))
        if media_id != 42:
            raise R1ServiceError("视频不存在", status_code=404)
        if user_id != 7:
            raise R1ServiceError("无权访问该视频", status_code=403)
        return object()


class _Checkpoint:
    def __init__(self) -> None:
        self.context = _source_context()
        self.chunks = (
            VideoChunk(
                start_ms=0,
                end_ms=300_000,
                segment_summary="复杂度说明",
                keywords=("算法", "复杂度"),
                raw_segments=self.context.segments,
                embedding=(0.1, 0.2),
            ),
        )
        self.writes: list[str] = []

    async def load_context(self, media_id: int):
        return self.context

    async def load_chunks(self, media_id: int):
        return self.chunks

    async def load_result(self, key):
        return None

    async def save_context(self, *_args, **_kwargs):
        self.writes.append("context")

    async def save_chunks(self, *_args, **_kwargs):
        self.writes.append("chunks")

    async def save_result(self, *_args, **_kwargs):
        self.writes.append("result")


class _LongContext:
    def __init__(self) -> None:
        self.calls: list[tuple[int | None, VideoContext, object]] = []

    async def search_evidence(self, media_id, context, *, chunks=None):
        self.calls.append((media_id, context, chunks))
        return (_retrieved_hit(),)


class _Chat:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def complete(self, messages, *, stage: str):
        self.calls.append((messages, stage))
        return _answer_json()


class _Telemetry:
    def __init__(self) -> None:
        self.scopes = 0

    @contextmanager
    def isolated_metrics(self):
        self.scopes += 1
        yield {}


async def _noop() -> None:
    return None


def _production_app(monkeypatch):
    services = ProductionR4Services.__new__(ProductionR4Services)
    services.auth = _Auth()
    services.media = _Media()
    services.checkpoint = _Checkpoint()
    long_context = _LongContext()
    chat = _Chat()
    telemetry = _Telemetry()
    services.providers = SimpleNamespace(
        long_context=long_context,
        chat_client=chat,
        telemetry=telemetry,
    )
    services.startup = _noop
    services.shutdown = _noop
    monkeypatch.setenv("DOVIDEO_PROFILE", "production")
    monkeypatch.setattr(
        "dovideo.presentation.api.r4_runtime.create_production_services",
        lambda: services,
    )
    return create_app(), services, long_context, chat, telemetry


def test_authenticated_production_follow_up_contract_owner_and_evidence_search(
    monkeypatch,
) -> None:
    app, services, long_context, chat, telemetry = _production_app(monkeypatch)

    with TestClient(app) as client:
        response = client.post(
            "/analysis/follow-up",
            params={
                "id": 42,
                "question": "为什么是线性复杂度？",
                "goal": "解释算法复杂度",
                "mode": "LEARNING",
            },
            headers={"Authorization": "Bearer 7"},
        )

        assert response.status_code == 200
        envelope = response.json()
        assert envelope["code"] == 0
        assert isinstance(envelope["data"], str)
        assert "算法的时间复杂度是 O(n)" in envelope["data"]
        assert "[00:00–00:10] ASR" in envelope["data"]
        assert services.media.calls == [(42, 7)]
        assert long_context.calls[0][1].user_goal == "为什么是线性复杂度？"
        assert long_context.calls[0][2] == services.checkpoint.chunks
        assert chat.calls[0][1] == "FOLLOW_UP"
        assert telemetry.scopes == 1
        assert services.checkpoint.writes == []

        # The existing same-video evidence-search endpoint still uses the
        # same production LongVideoContextService object.
        evidence = client.get(
            "/analysis/evidence-search",
            params={"id": 42, "query": "复杂度"},
            headers={"Authorization": "Bearer 7"},
        )
        assert evidence.status_code == 200
        assert evidence.json()["code"] == 0
        assert len(long_context.calls) == 2
        assert long_context.calls[1][1].user_goal == "复杂度"


def test_production_follow_up_rejects_cross_owner_missing_media_and_auto(
    monkeypatch,
) -> None:
    app, services, long_context, chat, _ = _production_app(monkeypatch)

    with TestClient(app) as client:
        cross_owner = client.post(
            "/analysis/follow-up",
            params={"id": 42, "question": "问题", "mode": "GENERAL"},
            headers={"Authorization": "Bearer 8"},
        )
        missing_media = client.post(
            "/analysis/follow-up",
            params={"id": 999, "question": "问题", "mode": "GENERAL"},
            headers={"Authorization": "Bearer 7"},
        )
        auto_mode = client.post(
            "/analysis/follow-up",
            params={"id": 42, "question": "问题", "mode": "AUTO"},
            headers={"Authorization": "Bearer 7"},
        )
        unauthenticated = client.post(
            "/analysis/follow-up",
            params={"id": 42, "question": "问题", "mode": "GENERAL"},
        )

    assert cross_owner.status_code == 403
    assert missing_media.status_code == 404
    assert auto_mode.status_code == 400
    assert unauthenticated.status_code == 401
    assert "O(n)" not in cross_owner.text + missing_media.text + auto_mode.text
    assert long_context.calls == []
    assert chat.calls == []
    assert services.checkpoint.writes == []


@pytest.mark.parametrize("valid_refs", [True, False])
def test_r6_production_follow_up_large_candidate_uses_grounding_http_semantics(monkeypatch, valid_refs):
    app, services, long_context, chat, _ = _production_app(monkeypatch)
    texts = tuple(f"irrelevant observation {index}" for index in range(11)) + (_retrieved_hit().transcript,)
    items = tuple(SourceItemIdentity(
        source_item_id=f"media-42-item-{index}", source_revision="media-42-revision",
        segment_id="media-42-segment", source_type="ASR", ordinal=index,
        timestamp_ms=0, end_ms=10000, content_digest=content_digest(text),
    ) for index, text in enumerate(texts))
    segment = VideoSegment(
        start_ms=0, end_ms=10000, transcript="\n".join(texts),
        source_revision="media-42-revision", segment_id="media-42-segment", source_items=items,
    )
    services.checkpoint.context = VideoContext(
        source="minio://media/video-42.mp4", source_revision=segment.source_revision, segments=(segment,),
    )
    hit = _retrieved_hit().model_copy(update={
        "source_revision": segment.source_revision, "segment_id": segment.segment_id,
        "source_item_ids": segment.source_item_ids if valid_refs else ("media-99-item",),
    })
    async def search(*args, **kwargs):
        return (hit,)
    long_context.search_evidence = search
    with TestClient(app) as client:
        response = client.post(
            "/analysis/follow-up", params={"id": 42, "question": "问题", "mode": "GENERAL"},
            headers={"Authorization": "Bearer 7"},
        )
    assert response.status_code == (200 if valid_refs else 422)
    assert response.json()["code"] == (0 if valid_refs else 422)
    assert len(chat.calls) == 1
    assert services.checkpoint.writes == []

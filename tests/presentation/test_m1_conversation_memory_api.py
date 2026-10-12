from uuid import uuid4
import json
import pytest
from fastapi.testclient import TestClient

from test_p3_grounded_follow_up_api import _production_app
from dovideo.application.conversation_memory import ConversationMemoryService
from dovideo.infrastructure.conversation_memory import InMemoryConversationMemoryStore
from dovideo.application import TaskKey
from dovideo.domain import AgentState, AnalysisEvidence, AnalysisMode, AnalysisResult, SourceItemIdentity, VideoSegment, VideoContext, content_digest


def app_with_memory(monkeypatch):
    app, services, retrieval, chat, telemetry = _production_app(monkeypatch)
    services.conversation_memory = ConversationMemoryService(InMemoryConversationMemoryStore())
    old_complete = chat.complete
    async def complete(messages, *, stage):
        if stage == "QUERY_REWRITE":
            chat.calls.append((messages, stage))
            return {"standalone_query": "视频中算法的空间复杂度是什么？",
                    "needs_clarification": False, "clarification_question": ""}
        return await old_complete(messages, stage=stage)
    chat.complete = complete
    return app, services, retrieval, chat


def params(conversation_id=None):
    return {"id": 42, "question": "算法复杂度是什么？", "goal": "解释算法复杂度",
            "mode": "LEARNING", "conversationId": conversation_id or str(uuid4()), "requestId": str(uuid4())}


def test_memory_api_preserves_string_response_dedup_and_owned_history(monkeypatch):
    app, services, retrieval, chat = app_with_memory(monkeypatch)
    p = params()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer 7"}
        answer = client.post("/analysis/follow-up", params=p, headers=headers)
        replay = client.post("/analysis/follow-up", params=p, headers=headers)
        assert answer.status_code == 200 and answer.json() == replay.json()
        assert isinstance(answer.json()["data"], str) and "视频证据" in answer.json()["data"]
        assert len(chat.calls) == 1
        p["question"], p["requestId"] = "它的复杂度呢？", str(uuid4())
        assert client.post("/analysis/follow-up", params=p, headers=headers).status_code == 200
        assert retrieval.calls[-1][1].user_goal == "视频中算法的空间复杂度是什么？"
        history = client.get("/analysis/follow-up/history", params=p, headers=headers)
        assert history.status_code == 200 and len(history.json()["data"]["turns"]) == 2
        assert history.json()["data"]["sourceRevision"].startswith("legacy-")
        for bad_headers, media_id, expected in [({}, 42, 401), ({"Authorization": "Bearer 8"}, 42, 403), (headers, 999, 404)]:
            result = client.get("/analysis/follow-up/history", params={**p, "id": media_id}, headers=bad_headers)
            assert result.status_code == expected and "O(n)" not in result.text
        for patch in [{"conversationId": str(uuid4())}, {"goal": "不同目标"}, {"mode": "REVIEW"}]:
            history = client.get("/analysis/follow-up/history", params={**p, **patch}, headers=headers)
            assert history.json()["data"]["turns"] == []


def test_invalid_ids_and_failed_guard_do_not_create_history(monkeypatch):
    app, services, retrieval, chat = app_with_memory(monkeypatch)
    p = params()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer 7"}
        for patch in [{"conversationId": "bad"}, {"requestId": "bad"}]:
            assert client.post("/analysis/follow-up", params={**p, **patch}, headers=headers).status_code == 400
        async def forged(messages, *, stage):
            return {"answer": "伪造事实", "evidence": [{"candidateIndex": 0, "timestampMs": 1200,
                "source": "ASR", "content": "伪造事实", "claim": "伪造事实"}]}
        chat.complete = forged
        assert client.post("/analysis/follow-up", params=p, headers=headers).status_code == 422
        assert client.get("/analysis/follow-up/history", params=p, headers=headers).json()["data"]["turns"] == []


def test_history_store_failure_is_explicit_and_contains_no_private_errors(monkeypatch):
    app, services, retrieval, chat = app_with_memory(monkeypatch)
    async def broken(identity): raise OSError("secret connection details")
    services.conversation_memory.store.load = broken
    with TestClient(app) as client:
        response = client.get("/analysis/follow-up/history", params=params(), headers={"Authorization": "Bearer 7"})
        assert response.status_code == 503 and "secret" not in response.text


@pytest.mark.parametrize("with_conversation", [False, True])
def test_first_ordinal_follow_up_api_keeps_original_question_and_string_contract(monkeypatch, with_conversation):
    # Real API/service/adapters/guard; fixture video and mock provider/retrieval.
    app, services, retrieval, chat = app_with_memory(monkeypatch)
    cp = services.checkpoint
    text = cp.context.segments[0].transcript
    item = SourceItemIdentity(source_item_id="api-initial-item", source_revision="api-r1",
        segment_id="api-segment", source_type="ASR", ordinal=0, timestamp_ms=0,
        end_ms=10000, content_digest=content_digest(text))
    segment = VideoSegment(start_ms=0, end_ms=10000, transcript=text,
        source_revision="api-r1", segment_id="api-segment", source_items=(item,))
    cp.context = VideoContext(source=cp.context.source, source_revision="api-r1", segments=(segment,))
    initial = AgentState(goal="解释算法复杂度", result=AnalysisResult(title="复杂度分析",
        conclusions=("时间复杂度是 O(n)", "每个元素只处理一次", "其余问题需要证据"),
        evidence=(AnalysisEvidence(timestamp_ms=1200, source="ASR", content=text, claim=text,
            source_revision="api-r1", segment_id="api-segment", source_item_ids=(item.source_item_id,)),)))
    async def load_result(key):
        assert key == TaskKey(42, "解释算法复杂度", AnalysisMode.LEARNING)
        return initial
    cp.load_result = load_result
    old_search = retrieval.search_evidence
    async def search(*args, **kwargs):
        hits = await old_search(*args, **kwargs)
        return tuple(hit.model_copy(update={"source_revision": "api-r1", "segment_id": "api-segment",
            "source_item_ids": (item.source_item_id,)}) for hit in hits)
    retrieval.search_evidence = search
    old_complete = chat.complete
    query = "视频中算法为什么每个元素只处理一次？"
    async def complete(messages, *, stage):
        if stage == "QUERY_REWRITE":
            chat.calls.append((messages, stage))
            payload = json.loads(messages[-1]["content"].split("Input as JSON:\n")[-1])
            assert payload["conversationContext"]["priorAnalysisContext"]["conclusions"][1] == "每个元素只处理一次"
            assert payload["conversationContext"]["recentTurns"] == []
            return {"standalone_query": query, "needs_clarification": False, "clarification_question": ""}
        return await old_complete(messages, stage=stage)
    chat.complete = complete
    p = {**params(), "question": "第二点呢？"}
    if not with_conversation:
        p.pop("conversationId")
        p.pop("requestId")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer 7"}
        answer = client.post("/analysis/follow-up", params=p, headers=headers)
        assert answer.status_code == 200 and isinstance(answer.json()["data"], str)
        assert "视频证据" in answer.json()["data"]
        assert retrieval.calls[0][1].user_goal == query
        assert [stage for _, stage in chat.calls] == ["QUERY_REWRITE", "FOLLOW_UP"]
        final_payload = json.loads(chat.calls[-1][0][-1]["content"].split("Input as JSON:\n")[-1])
        assert final_payload["question"] == "第二点呢？"
        if with_conversation:
            history = client.get("/analysis/follow-up/history", params=p, headers=headers).json()["data"]
            assert len(history["turns"]) == 1 and history["turns"][0]["question"] == "第二点呢？"
        assert cp.writes == []

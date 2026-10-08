from uuid import uuid4
from fastapi.testclient import TestClient

from test_p3_grounded_follow_up_api import _production_app
from dovideo.application.conversation_memory import ConversationMemoryService
from dovideo.infrastructure.conversation_memory import InMemoryConversationMemoryStore


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

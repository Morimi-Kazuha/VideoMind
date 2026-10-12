import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from test_m1_conversation_memory_api import app_with_memory, params
from dovideo.application.retrieval_need import RetrievalNeedSettings, RetrievalNeedMode
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig, ProviderHttpResponse


@pytest.mark.parametrize("fault", [None, "knowledge", "deleted"])
def test_production_mixed_string_history_replay_auth_and_failure(monkeypatch, fault):
    app, services, retrieval, old_chat = app_with_memory(monkeypatch)
    services.retrieval_need_settings = RetrievalNeedSettings(RetrievalNeedMode.ENABLED)
    services.mixed_follow_up_enabled = True
    services.conversation_memory.store.write_sources = True
    media_state = {"deleted": False}
    original_access = services.media.require_owned
    async def access(*args):
        if media_state["deleted"]:
            from dovideo.presentation.api.runtime import R1ServiceError
            raise R1ServiceError("视频不存在", status_code=404)
        return await original_access(*args)
    services.media.require_owned = access
    class Http:
        calls = []
        async def post(self, url, **kw):
            payload = kw["json"]
            knowledge = "source quotes" in payload["messages"][0]["content"]
            self.calls.append("MIXED_KNOWLEDGE" if knowledge else "FOLLOW_UP")
            if knowledge:
                content = {"knowledge_blocks": [{"kind": "INFERENCE", "text": "单次遍历通常是线性复杂度，但仍需确认循环和输入规模。"}]}
                if fault == "knowledge": content["evidence"] = []
                if fault == "deleted":
                    media_state["deleted"] = True
            else:
                content = {"answer": "算法的时间复杂度是 O(n)", "evidence": [{"candidateIndex": 0, "timestampMs": 1200,
                    "source": "ASR", "content": "算法的时间复杂度是 O(n)", "claim": "算法的时间复杂度是 O(n)"}]}
            return ProviderHttpResponse(200, {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}], "usage": {"total_tokens": 20}})
    http = Http()
    services.providers.chat_client = OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1", model="fixture", max_attempts=1), client=http)
    p = {**params(), "question": "作者解释的时间复杂度正确吗？"}
    headers = {"Authorization": "Bearer 7"}
    with TestClient(app) as client:
        for auth, expected in [({}, 401), ({"Authorization": "Bearer 8"}, 403)]:
            assert client.post("/analysis/follow-up", params=p, headers=auth).status_code == expected
        assert http.calls == []
        result = client.post("/analysis/follow-up", params=p, headers=headers)
        assert result.status_code == ({None: 200, "knowledge": 502, "deleted": 404}[fault])
        if fault is None:
            answer = result.json()["data"]
            assert isinstance(answer, str) and "## 视频事实" in answer and "## 通用知识与推理" in answer
            assert "[00:00–00:10] ASR" in answer
            assert client.post("/analysis/follow-up", params=p, headers=headers).json() == result.json()
            services.mixed_follow_up_enabled = False
            history = client.get("/analysis/follow-up/history", params=p, headers=headers).json()["data"]
            assert history["turns"][0]["answer_kind"] == "MIXED" and history["turns"][0]["answer"] == answer
        elif fault == "knowledge":
            assert client.get("/analysis/follow-up/history", params=p, headers=headers).json()["data"]["turns"] == []
    assert http.calls == ["FOLLOW_UP", "MIXED_KNOWLEDGE"] and len(retrieval.calls) == 1

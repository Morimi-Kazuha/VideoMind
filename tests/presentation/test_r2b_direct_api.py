import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from test_m1_conversation_memory_api import app_with_memory, params
from dovideo.application.retrieval_need import RetrievalNeedSettings, RetrievalNeedMode
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig, ProviderHttpResponse


@pytest.mark.parametrize("bad", [False, True])
def test_production_direct_string_history_replay_and_auth(monkeypatch, bad):
    app, services, retrieval, chat = app_with_memory(monkeypatch)
    services.retrieval_need_settings = RetrievalNeedSettings(RetrievalNeedMode.ENABLED)
    services.conversation_memory.store.write_sources = True
    class Http:
        calls = 0
        async def post(self, url, **kwargs):
            self.calls += 1
            content = {"answer": "Redis 常用于缓存。"}
            if bad: content["answer_kind"] = "VIDEO_GROUNDED"
            return ProviderHttpResponse(200, {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})
    http = Http()
    services.providers.chat_client = OpenAICompatibleChatClient(
        ProviderConfig(base_url="https://example.test/v1", model="fixture", max_attempts=1), client=http)
    p = {**params(), "question": "不考虑当前视频，解释 Redis。"}
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer 7"}
        for auth, expected in [({}, 401), ({"Authorization": "Bearer 8"}, 403)]:
            assert client.post("/analysis/follow-up", params=p, headers=auth).status_code == expected
        assert http.calls == 0
        first = client.post("/analysis/follow-up", params=p, headers=headers)
        assert first.status_code == (502 if bad else 200)
        if not bad:
            assert first.json()["data"] == "Redis 常用于缓存。"
            assert isinstance(first.json()["data"], str)
            assert client.post("/analysis/follow-up", params=p, headers=headers).json() == first.json()
        history = client.get("/analysis/follow-up/history", params=p, headers=headers).json()["data"]
    assert retrieval.calls == [] and http.calls == 1
    assert history["turns"] == [] if bad else history["turns"][0]["answer_kind"] == "MODEL_KNOWLEDGE"

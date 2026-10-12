from fastapi.testclient import TestClient
import pytest
from dovideo.application.follow_up_budget import FollowUpBudgetMode as Mode, FollowUpBudgetSettings as Settings
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig, ProviderHttpResponse
from test_p3_grounded_follow_up_api import _production_app, _answer_json


@pytest.mark.parametrize("mode", [Mode.OFF, Mode.SHADOW, Mode.ENFORCED])
def test_production_boundary_string_success_or_safe_admission_rejection(monkeypatch, mode):
    app, services, retrieval, old_chat, telemetry = _production_app(monkeypatch)
    class Http:
        calls = 0
        async def post(self, url, **kwargs):
            self.calls += 1
            return ProviderHttpResponse(200, {"choices": [{"message": {"content": _answer_json()}}],
                                              "usage": {"total_tokens": 20}})
    http = Http()
    services.providers.chat_client = OpenAICompatibleChatClient(
        ProviderConfig(base_url="https://example.test/v1", model="test", max_attempts=1), client=http)
    services.follow_up_budget_settings = Settings(mode, 1)
    with TestClient(app) as client:
        result = client.post("/analysis/follow-up", params={"id": 42, "question": "算法为何是线性复杂度？", "mode": "LEARNING"},
                             headers={"Authorization": "Bearer 7"})
    assert len(retrieval.calls) == 1 and services.checkpoint.writes == []
    if mode == Mode.ENFORCED:
        assert result.status_code == 503 and result.json()["code"] == 503 and http.calls == 0
        assert "预算" in result.json()["message"]
    else:
        assert result.status_code == 200 and http.calls == 1
        assert result.json()["code"] == 0 and isinstance(result.json()["data"], str)
        assert "[00:00–00:10] ASR" in result.json()["data"]


@pytest.mark.parametrize("write_sources", [False, True])
def test_history_serialization_is_legacy_by_default_and_tags_are_readable(monkeypatch, write_sources):
    from test_m1_conversation_memory_api import app_with_memory, params
    app, services, retrieval, chat = app_with_memory(monkeypatch)
    services.conversation_memory.store.write_sources = write_sources
    p = params()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer 7"}
        first = client.post("/analysis/follow-up", params=p, headers=headers)
        replay = client.post("/analysis/follow-up", params=p, headers=headers)
        assert first.json() == replay.json() and len(chat.calls) == 1
        history = client.get("/analysis/follow-up/history", params=p, headers=headers).json()["data"]
    turn = history["turns"][0]
    if write_sources:
        assert turn["answer_kind"] == "VIDEO_GROUNDED"
    else:
        assert "answer_kind" not in turn

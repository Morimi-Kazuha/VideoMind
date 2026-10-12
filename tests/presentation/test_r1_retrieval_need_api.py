"""Production Follow-up composition and legacy HTTP behavior with R1 shadow."""
import pytest
from fastapi.testclient import TestClient

from test_p3_grounded_follow_up_api import _production_app
from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings


@pytest.mark.parametrize("question", ["不考虑当前视频，解释 Redis。", "作者讲了什么？", "作者解释正确吗？"])
def test_production_shadow_keeps_http_and_provider_contract(monkeypatch, caplog, question):
    results = []
    for mode in (RetrievalNeedMode.OFF, RetrievalNeedMode.SHADOW):
        app, services, retrieval, chat, telemetry = _production_app(monkeypatch)
        services.retrieval_need_settings = RetrievalNeedSettings(mode)
        with caplog.at_level("INFO", logger="dovideo.retrieval_need"), TestClient(app) as client:
            response = client.post("/analysis/follow-up", params={"id": 42, "question": question, "mode": "LEARNING"},
                headers={"Authorization": "Bearer 7"})
        results.append((response.status_code, response.json(), retrieval.calls, chat.calls, telemetry.scopes))
        assert services.checkpoint.writes == []
    assert results[0] == results[1]
    assert results[1][0] == 200 and isinstance(results[1][1]["data"], str)
    assert len(results[1][2]) == len(results[1][3]) == results[1][4] == 1
    assert len([r for r in caplog.records if r.name == "dovideo.retrieval_need"]) == 1


def test_rejected_identity_and_bad_request_do_not_shadow(monkeypatch, caplog):
    app, services, retrieval, chat, _ = _production_app(monkeypatch)
    services.retrieval_need_settings = RetrievalNeedSettings(RetrievalNeedMode.SHADOW)
    with caplog.at_level("INFO", logger="dovideo.retrieval_need"), TestClient(app) as client:
        for media, token, mode, code in ((42, "8", "GENERAL", 403), (999, "7", "GENERAL", 404),
            (42, "7", "AUTO", 400), (42, None, "GENERAL", 401)):
            response = client.post("/analysis/follow-up", params={"id": media, "question": "作者观点？", "mode": mode},
                headers={"Authorization": "Bearer " + token} if token else {})
            assert response.status_code == code
    assert retrieval.calls == chat.calls == []
    assert not [r for r in caplog.records if r.name == "dovideo.retrieval_need"]

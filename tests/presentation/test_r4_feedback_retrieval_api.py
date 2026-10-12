"""Actual production Follow-up assembly and API, all provider ports offline."""
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from r4_validation_support import harness, insufficient, FeedbackRetrievalMode
from test_m1_conversation_memory_api import app_with_memory, params
from dovideo.application.evidence_reflection import ReflectionMode
from dovideo.application.follow_up_budget import FollowUpBudgetMode, FollowUpBudgetSettings
from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings
from dovideo.application.retrieval_repair import capture_feedback_retrieval
from dovideo.application.retrieval_budget import capture_retrieval_budget


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [FeedbackRetrievalMode.OFF, FeedbackRetrievalMode.SHADOW, FeedbackRetrievalMode.ENABLED])
async def test_server_switch_actual_wiring_string_contract_permissions_and_receipt(monkeypatch, mode):
    question = "作者为什么选择 B？"
    fixture = await harness(question, insufficient(question))
    app, services, _, _ = app_with_memory(monkeypatch)
    services.checkpoint = fixture[0]._checkpoint
    services.providers.long_context = fixture[0]._retrieval
    services.providers.chat_client = fixture[0]._model._chat
    services.evidence_reflection_mode = ReflectionMode.SHADOW
    services.feedback_retrieval_mode = mode
    services.follow_up_budget_settings = FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 500000)
    services.retrieval_need_settings = RetrievalNeedSettings(RetrievalNeedMode.ENABLED)
    services.mixed_follow_up_enabled = True
    services.conversation_memory.store.write_sources = True
    request = {**params(), "question": question, "feedbackMode": "ENABLED", "repairRounds": 99}
    with TestClient(app) as client:
        assert client.post("/analysis/follow-up", params=request).status_code == 401
        assert client.post("/analysis/follow-up", params=request, headers={"Authorization": "Bearer 8"}).status_code == 403
        assert not fixture[4]
        with capture_feedback_retrieval() as repairs, capture_retrieval_budget() as budgets:
            response = client.post("/analysis/follow-up", params=request, headers={"Authorization": "Bearer 7"})
        assert response.status_code == 200 and isinstance(response.json()["data"], str)
        counts = fixture[4].copy()
        assert client.post("/analysis/follow-up", params=request, headers={"Authorization": "Bearer 7"}).json() == response.json()
        assert fixture[4] == counts
        history = client.get("/analysis/follow-up/history", params=request, headers={"Authorization": "Bearer 7"}).json()["data"]
        assert len(history["turns"]) == 1 and history["turns"][0]["answer_kind"] == "VIDEO_GROUNDED"
        assert history["sourceRevision"] == fixture[1].source_revision
    assert fixture[4]["reflection_http"] == 1
    assert fixture[4]["planner_logical"] == (2 if mode == FeedbackRetrievalMode.ENABLED else 1)
    if mode == FeedbackRetrievalMode.ENABLED:
        assert repairs[0].state == "EVIDENCE_ADDED" and budgets[0]["repair_base_calls"] == 1
        assert "减少内存占用" in response.json()["data"]
    elif mode == FeedbackRetrievalMode.SHADOW:
        assert repairs[0].state == "SHADOW_PLANNED"
    else:
        assert not repairs and not budgets  # client parameters cannot enable R4

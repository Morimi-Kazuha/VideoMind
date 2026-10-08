import asyncio
import json
import pytest
from pydantic import ValidationError

from dovideo.application.conversation_memory import QueryRewrite, RollingSummary
from dovideo.application.execution_budget import AgentExecutionBudget
from dovideo.infrastructure.providers.conversation_memory import ConversationModelAdapter
from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient
from dovideo.infrastructure.providers.config import ProviderConfig, ModelRequestSettings
from dovideo.infrastructure.providers.http import ProviderHttpResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,cap", [("QUERY_REWRITE", 512), ("ROLLING_SUMMARY", 2048)])
async def test_roles_share_provider_json_protocol_deadline_output_and_usage(stage, cap):
    calls, usage, admissions = [], [], []
    class Http:
        async def post(self, url, *, headers, json, timeout):
            calls.append((json, timeout))
            return ProviderHttpResponse(200, {"choices": [{"message": {"content": "{}"}}],
                                             "usage": {"total_tokens": 42}})
    class Usage:
        def record_chat_usage(self, **kwargs): usage.append(kwargs)
        def admit_model_call(self, **kwargs): admissions.append(kwargs); return {}
    # Provider telemetry is already covered by existing isolated usage tests;
    # here inspect the exact transport controls for the two new stages.
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="https://provider.invalid/v1", model="mock"),
        client=Http(), request_settings=ModelRequestSettings(max_tokens=16000), usage_sink=Usage())
    with AgentExecutionBudget.open(2000):
        await chat.complete(({"role": "user", "content": "bounded"},), stage=stage)
    assert calls[0][0]["max_tokens"] == cap
    assert calls[0][0]["response_format"] == {"type": "json_object"}
    assert 0 < calls[0][1] <= 2
    assert usage[0]["stage"] == stage and usage[0]["total_tokens"] == 42
    assert admissions[0]["max_output_tokens"] == cap


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"standalone_query": "Redis", "needs_clarification": "false", "clarification_question": ""},
    {"standalone_query": "Redis", "needs_clarification": False, "clarification_question": "", "extra": 1},
    {"standalone_query": "x"*501, "needs_clarification": False, "clarification_question": ""},
    {"standalone_query": "Redis", "needs_clarification": True, "clarification_question": "对象？"},
])
async def test_invalid_rewrite_protocol_rejected(payload):
    class Chat:
        async def complete(self, messages, *, stage): return payload
    with pytest.raises(ValidationError): await ConversationModelAdapter(Chat()).rewrite("它呢？", {})


@pytest.mark.asyncio
async def test_role_obeys_outer_budget_and_cancels_stalled_provider():
    cancelled = []
    class Chat:
        async def complete(self, messages, *, stage):
            try: await asyncio.Future()
            finally: cancelled.append(stage)
    with AgentExecutionBudget.open(10):
        with pytest.raises(TimeoutError): await ConversationModelAdapter(Chat()).rewrite("它呢？", {})
    assert cancelled == ["QUERY_REWRITE"]

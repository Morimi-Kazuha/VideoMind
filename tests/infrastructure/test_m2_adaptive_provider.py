import json

import pytest

from dovideo.application.adaptive_retrieval import AdaptiveRetrievalSettings
from dovideo.infrastructure.adaptive_retrieval import adaptive_settings_from_environment
from dovideo.infrastructure.providers.model import RetrievalPlannerModelAdapter


@pytest.mark.asyncio
async def test_provider_uses_existing_chat_stage_strict_json_and_data_boundary():
    class Chat:
        async def complete(self, messages, *, stage):
            assert stage == "ADAPTIVE_RETRIEVAL_PLANNER"
            assert "untrusted" in messages[0]["content"]
            data = json.loads(messages[-1]["content"].split("Input as JSON:\n")[1])
            assert data == {"question": "最初 MySQL；后来 Redis"}
            return '{"retrieval_route":"BOUNDED_MULTI_QUERY","reason_code":"TEMPORAL_CHANGE","sub_queries":["最初 MySQL","后来 Redis"]}'
    result = await RetrievalPlannerModelAdapter(Chat()).suggest_retrieval("最初 MySQL；后来 Redis")
    assert len(result.sub_queries) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["```json\n{}\n```", "[]", "x" * 8193,
    '{"retrieval_route":"SINGLE_HYBRID","reason_code":"SINGLE_FACT","reason_code":"COMPARISON","sub_queries":[]}',
    {"retrieval_route": "SINGLE_HYBRID", "reason_code": "SINGLE_FACT", "sub_queries": [], "media_id": 2},
    {"retrieval_route": "ANY_TOOL", "reason_code": "SINGLE_FACT", "sub_queries": []},
    {"retrieval_route": "BOUNDED_MULTI_QUERY", "reason_code": "COMPARISON", "sub_queries": [1, 2]}])
async def test_provider_rejects_unknown_fields_tools_types_and_non_json(payload):
    class Chat:
        async def complete(self, messages, *, stage): return payload
    with pytest.raises(ValueError):
        await RetrievalPlannerModelAdapter(Chat()).suggest_retrieval("compare A B")


@pytest.mark.parametrize("kwargs", [{"max_queries": 4}, {"max_queries": True}, {"max_candidates": 9},
    {"planning_timeout_seconds": float("nan")}, {"planning_timeout_seconds": float("inf")},
    {"planning_timeout_seconds": 11}, {"enabled": "true"}])
def test_settings_cannot_widen_bounds(kwargs):
    with pytest.raises(ValueError): AdaptiveRetrievalSettings(**kwargs)


def test_configuration_off_by_default_and_invalid_environment_fails_closed():
    assert not adaptive_settings_from_environment({}).enabled
    assert adaptive_settings_from_environment({"DOVIDEO_ADAPTIVE_RETRIEVAL_ENABLED": "true"}).enabled
    for env in ({"DOVIDEO_ADAPTIVE_RETRIEVAL_ENABLED": "yes"},
                {"DOVIDEO_ADAPTIVE_RETRIEVAL_MAX_QUERIES": "4"},
                {"DOVIDEO_ADAPTIVE_RETRIEVAL_MAX_CANDIDATES": "9"}):
        with pytest.raises(ValueError): adaptive_settings_from_environment(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("adaptive", [False, True])
@pytest.mark.parametrize("tools", [False, True])
async def test_production_composes_shared_service_without_enabling_tools(monkeypatch, adaptive, tools):
    import dovideo.infrastructure.r4_runtime as runtime
    from dovideo.application.adaptive_retrieval import AdaptiveRetrievalService
    from dovideo.infrastructure.providers.config import ProviderConfig
    from dovideo.infrastructure.model_routing import ModelRoutingProductionSettings
    from dovideo.infrastructure.x1_config import X1ToolCallingSettings
    monkeypatch.setenv("DOVIDEO_ADAPTIVE_RETRIEVAL_ENABLED", str(adaptive).lower())
    monkeypatch.setattr(runtime.ProviderConfig, "from_environment", classmethod(
        lambda cls, *args, **kwargs: ProviderConfig(base_url="https://chat.invalid/v1", model="chat")))
    monkeypatch.setattr(runtime, "embedding_provider_config_from_environment", lambda **kwargs:
        ProviderConfig(base_url="https://embedding.invalid/v1", model="BAAI/bge-m3", embedding_model="BAAI/bge-m3"))
    stack = runtime.create_r4_provider_stack(object(), object(), runtime.R4AgentTelemetry(object()), None,
        routing_settings=ModelRoutingProductionSettings(enabled=False), tool_settings=X1ToolCallingSettings(enabled=tools))
    try:
        retrieval = stack.long_context._retrieval
        assert isinstance(retrieval, AdaptiveRetrievalService) is adaptive
        assert isinstance(retrieval.baseline if adaptive else retrieval, runtime._StrictRetrievalService)
        assert (stack.tool_executor is not None) is tools
        if tools: assert stack.tool_executor._search_service is stack.long_context
    finally:
        await stack.close()


@pytest.mark.asyncio
async def test_planner_transport_caps_tokens_admission_and_attempts():
    from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient
    from dovideo.infrastructure.providers.config import ProviderConfig, ModelRequestSettings
    from dovideo.infrastructure.providers.http import ProviderHttpResponse
    from dovideo.infrastructure.providers.errors import ProviderTransientError
    class Client:
        calls = 0
        async def post(self, url, *, headers, json, timeout):
            self.calls += 1
            assert json["max_tokens"] == 1024
            return ProviderHttpResponse(503, {})
    class Sink:
        def admit_model_call(self, **values):
            assert values["max_output_tokens"] == 1024
    client = Client()
    chat = OpenAICompatibleChatClient(ProviderConfig(base_url="http://mock.invalid/v1", model="fixture", max_attempts=3),
        client=client, request_settings=ModelRequestSettings(max_tokens=8192), usage_sink=Sink())
    with pytest.raises(ProviderTransientError):
        await chat.complete([{"role": "user", "content": "fixture"}], stage="ADAPTIVE_RETRIEVAL_PLANNER")
    assert client.calls == 1

"""Stage-only configuration, strict failures and shared admission invariants."""
import asyncio
import json

import pytest

from dovideo.infrastructure.evidence_reflection import reflection_request_settings_from_environment
from dovideo.infrastructure.providers.config import ModelRequestSettings, ProviderConfig
from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter
from dovideo.infrastructure.providers.http import ProviderHttpResponse
from dovideo.infrastructure.providers.errors import ProviderRequestError
from dovideo.application.evidence_reflection import EvidenceReflection, ReflectionMode, capture_evidence_reflection
from dovideo.application.follow_up_budget import (
    FollowUpBudgetSettings, FollowUpBudgetMode, capture_follow_up_budget, follow_up_budget_scope,
)
from dovideo.application.execution_budget import AgentExecutionBudget


def config(**changes):
    return ProviderConfig(**{**dict(base_url="https://openrouter.ai/api/v1", model="deepseek/deepseek-v4.1-flash",
        api_key="fixture-key", transport="openrouter", provider_only=("siliconflow/fp8",),
        provider_data_collection="deny", provider_zdr=True, max_attempts=3), **changes})


UNKNOWN = '{"status":"UNKNOWN","reason_code":"UNCERTAIN_SEMANTIC_SUPPORT","covered_requirements":[],"missing_requirements":[],"candidate_references":[]}'


class Http:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    async def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        return ProviderHttpResponse(self.status, {"choices": [{"message": {"content": UNKNOWN}, "finish_reason": "stop"}],
                                                 "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


def test_env_default_and_disable_preserve_original_behavior():
    assert reflection_request_settings_from_environment({}) is None
    assert reflection_request_settings_from_environment({"DOVIDEO_EVIDENCE_REFLECTION_REASONING_EFFORT": "  "}) is None
    setting = reflection_request_settings_from_environment({"DOVIDEO_EVIDENCE_REFLECTION_REASONING_EFFORT": "none"})
    assert setting == ModelRequestSettings(reasoning_effort="none")


@pytest.mark.parametrize("value", ["low", "minimal", "high", "NONE", False, 0, "bogus"])
def test_unverified_setting_is_rejected_deterministically(value):
    with pytest.raises(ValueError):
        reflection_request_settings_from_environment({"DOVIDEO_EVIDENCE_REFLECTION_REASONING_EFFORT": value})


@pytest.mark.parametrize("setting", [ModelRequestSettings(reasoning_effort="low"), ModelRequestSettings(reasoning_effort="none", max_tokens=2048), {}, "none"])
def test_override_does_not_introduce_new_cap_or_unverified_effort(setting):
    with pytest.raises(ValueError):
        OpenAICompatibleChatClient(config(), reflection_request_settings=setting)


@pytest.mark.parametrize("changes", [{"model": "deepseek/other-model"}, {"provider_only": ("nextbit/fp8",)}])
def test_unverified_model_or_provider_cannot_silently_accept_override(changes):
    with pytest.raises(ValueError, match="validated model"):
        OpenAICompatibleChatClient(config(**changes), reflection_request_settings=ModelRequestSettings(reasoning_effort="none"))


@pytest.mark.asyncio
async def test_only_reflection_overrides_profile_and_disabling_restores_payload():
    profile = ModelRequestSettings(reasoning_effort="high", max_tokens=16000)
    base_http, new_http = Http(), Http()
    base = OpenAICompatibleChatClient(config(), request_settings=profile, client=base_http)
    changed = OpenAICompatibleChatClient(config(), request_settings=profile, client=new_http,
        reflection_request_settings=ModelRequestSettings(reasoning_effort="none"))
    for stage in ("EVIDENCE_REFLECTION", "FOLLOW_UP", "DIRECT_FOLLOW_UP", "MIXED_KNOWLEDGE", "QUERY_REWRITE",
                  "ROLLING_SUMMARY", "ADAPTIVE_RETRIEVAL_PLANNER", "PLANNER", "EXECUTOR", "CRITIC", "MODE_ROUTER"):
        messages = [{"role": "user", "content": "Return JSON."}]
        await base.complete(messages, stage=stage)
        await changed.complete(messages, stage=stage)
        if stage == "EVIDENCE_REFLECTION":
            assert new_http.calls[-1] == {**base_http.calls[-1], "reasoning_effort": "none"}
            assert new_http.calls[-1]["max_tokens"] == 1024
        else:
            assert new_http.calls[-1] == base_http.calls[-1]
    assert changed.request_settings is profile


@pytest.mark.asyncio
async def test_stage_override_matches_measured_experiment_wire_and_both_reserves():
    messages = [{"role": "user", "content": "Return JSON."}]
    experimental_http, stage_http = Http(), Http()
    experiment = OpenAICompatibleChatClient(config(), request_settings=ModelRequestSettings(reasoning_effort="none", max_tokens=1024), client=experimental_http)
    admitted = []
    class Sink:
        def admit_model_call(self, **kwargs):
            admitted.append(kwargs)
    stage = OpenAICompatibleChatClient(config(), reflection_request_settings=ModelRequestSettings(reasoning_effort="none"), client=stage_http, usage_sink=Sink())
    await experiment.complete(messages, stage="EVIDENCE_REFLECTION")
    with capture_follow_up_budget() as records, follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 10000)):
        await stage.complete(messages, stage="EVIDENCE_REFLECTION")
    assert stage_http.calls[0] == experimental_http.calls[0]
    assert admitted[0]["max_output_tokens"] == 1024
    assert records[0]["events"][0]["output_reserve"] == 1024
    assert records[0]["charged_chat_tokens"] == 150
    assert records[0]["reserved_chat_tokens"] == 0


@pytest.mark.asyncio
async def test_profile_smaller_cap_preserved_in_wire_request_and_telemetry():
    admitted = []
    class Sink:
        def admit_model_call(self, **kwargs): admitted.append(kwargs)
    http = Http()
    client = OpenAICompatibleChatClient(config(), request_settings=ModelRequestSettings(max_tokens=512),
        reflection_request_settings=ModelRequestSettings(reasoning_effort="none"), client=http, usage_sink=Sink())
    with capture_follow_up_budget() as records, follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 10000)):
        await client.complete([{"role": "user", "content": "JSON"}], stage="EVIDENCE_REFLECTION")
    assert http.calls[0]["max_tokens"] == admitted[0]["max_output_tokens"] == records[0]["events"][0]["output_reserve"] == 512


@pytest.mark.asyncio
async def test_provider_rejection_is_one_attempt_no_default_reasoning_retry_and_shadow_unknown():
    http = Http(400)
    client = OpenAICompatibleChatClient(config(), reflection_request_settings=ModelRequestSettings(reasoning_effort="none"), client=http)
    adapter = EvidenceReflectionModelAdapter(client)
    candidates = ({"candidateIndex": 0, "asrExcerpt": "bounded fixture"},)
    messages = adapter.messages("why?", candidates)
    with AgentExecutionBudget.open(60000), follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 10000)), capture_evidence_reflection() as records:
        await EvidenceReflection(adapter, ReflectionMode.SHADOW).observe("why?", candidates, messages=messages, answer_reserve=0, time_reserve=0)
    assert len(http.calls) == 1 and http.calls[0]["reasoning_effort"] == "none"
    assert records[0].outcome == "PROVIDER_FAILURE" and records[0].assessment.status == "UNKNOWN"


@pytest.mark.asyncio
async def test_off_has_no_model_attempt_with_override():
    http = Http()
    adapter = EvidenceReflectionModelAdapter(OpenAICompatibleChatClient(config(), client=http,
        reflection_request_settings=ModelRequestSettings(reasoning_effort="none")))
    await EvidenceReflection(adapter, ReflectionMode.OFF).observe("question", ({"candidateIndex": 0},))
    assert not http.calls


@pytest.mark.asyncio
async def test_production_stack_wires_only_balanced_reflection_setting(monkeypatch):
    import dovideo.infrastructure.r4_runtime as runtime
    from dovideo.infrastructure.model_routing import ModelRoutingProductionSettings
    from dovideo.infrastructure.x1_config import X1ToolCallingSettings
    from dovideo.application.model_routing import ModelRouteLane
    monkeypatch.setenv("DOVIDEO_EVIDENCE_REFLECTION_REASONING_EFFORT", "none")
    monkeypatch.setenv("DOVIDEO_ADAPTIVE_RETRIEVAL_ENABLED", "false")
    monkeypatch.setattr(runtime.ProviderConfig, "from_environment", classmethod(lambda cls, *a, **kw: config()))
    monkeypatch.setattr(runtime, "embedding_provider_config_from_environment", lambda **kw:
        ProviderConfig(base_url="https://embedding.invalid/v1", model="BAAI/bge-m3", embedding_model="BAAI/bge-m3"))
    http = Http()
    stack = runtime.create_r4_provider_stack(object(), object(), runtime.R4AgentTelemetry(object()), None,
        routing_settings=ModelRoutingProductionSettings(enabled=False), tool_settings=X1ToolCallingSettings(enabled=False),
        model_http_clients={ModelRouteLane.BALANCED: http})
    try:
        assert stack.jev_router is None and stack.tool_executor is None
        assert stack.chat_client.request_settings == ModelRequestSettings()
        assert stack.chat_client.reflection_request_settings == ModelRequestSettings(reasoning_effort="none")
        await stack.chat_client.complete([{"role": "user", "content": "JSON"}], stage="EVIDENCE_REFLECTION")
        await stack.chat_client.complete([{"role": "user", "content": "JSON"}], stage="FOLLOW_UP")
        assert http.calls[0]["reasoning_effort"] == "none" and "reasoning_effort" not in http.calls[1]
    finally:
        await stack.close()


@pytest.mark.asyncio
async def test_concurrent_request_settings_and_ledgers_do_not_cross():
    async def request(override):
        http = Http()
        client = OpenAICompatibleChatClient(config(), request_settings=ModelRequestSettings(reasoning_effort="high"),
            reflection_request_settings=ModelRequestSettings(reasoning_effort="none") if override else None, client=http)
        with capture_follow_up_budget() as captured, follow_up_budget_scope(FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED, 10000)):
            await asyncio.sleep(0)
            await client.complete([{"role": "user", "content": "JSON"}], stage="EVIDENCE_REFLECTION")
        return http.calls[0], captured[0]
    a, b = await asyncio.gather(request(True), request(False))
    assert a[0]["reasoning_effort"] == "none" and b[0]["reasoning_effort"] == "high"
    assert all(snapshot["chat_http_attempts"] == 1 and snapshot["charged_chat_tokens"] == 150 for _, snapshot in (a, b))

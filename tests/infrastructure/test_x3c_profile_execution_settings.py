import pytest
from pydantic import ValidationError

from dovideo.application import (
    ModelRouteLane,
    RoutingSuggestion,
    TaskRoutingContext,
)
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AnalysisMode
from dovideo.infrastructure.model_routing import (
    ModelRoutingConfigurationError,
    ModelRoutingProductionSettings,
)
from dovideo.infrastructure.providers import (
    JevModelRouter,
    ModelRequestSettings,
    OpenAICompatibleChatClient,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderHttpResponse,
)


class _CapturingHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post(self, url, *, headers, json, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "json": json, "timeout": timeout}
        )
        return ProviderHttpResponse(
            200,
            {"choices": [{"message": {"content": "{}"}}]},
        )


def _profile_settings() -> ModelRoutingProductionSettings:
    return ModelRoutingProductionSettings(
        enabled=True,
        fast_model="deepseek-flash",
        balanced_model="deepseek-flash",
        deep_model="deepseek-v4-pro",
        fast_reasoning_effort="none",
        deep_reasoning_effort="max",
        deep_max_tokens=65_536,
    )


def test_same_model_lanes_have_distinct_stable_effective_profile_identities() -> None:
    settings = _profile_settings()
    fast = settings.effective_profile_identity(ModelRouteLane.FAST)
    balanced = settings.effective_profile_identity(ModelRouteLane.BALANCED)
    deep = settings.effective_profile_identity(ModelRouteLane.DEEP)

    assert fast.resolved_model_id == balanced.resolved_model_id == "deepseek-flash"
    assert fast.profile_id == "fast-profile"
    assert balanced.profile_id == "balanced-profile"
    assert fast.reasoning_effort == "none"
    assert fast.reasoning_mode == "disabled"
    assert balanced.reasoning_effort is None
    assert balanced.reasoning_mode == "provider-default"
    assert fast.fingerprint != balanced.fingerprint
    assert fast.fingerprint == settings.effective_profile_identity(
        ModelRouteLane.FAST
    ).fingerprint

    assert deep.profile_id == "deep-profile"
    assert deep.resolved_model_id == "deepseek-v4-pro"
    assert deep.reasoning_effort == "max"
    assert deep.max_tokens == 65_536
    assert "api_key" not in fast.as_dict()


def test_identical_model_request_profiles_are_rejected_when_routing_is_enabled() -> None:
    with pytest.raises(
        ModelRoutingConfigurationError,
        match="identical model request settings",
    ):
        ModelRoutingProductionSettings(
            enabled=True,
            fast_model="deepseek-flash",
            balanced_model="deepseek-flash",
        )


def test_environment_resolves_model_and_effort_per_lane_without_aliasing() -> None:
    values = {
        "DOVIDEO_MODEL_ROUTING_ENABLED": "true",
        "DOVIDEO_JEV_ENDPOINT": "https://jev.invalid/v1/systemone",
        "DOVIDEO_JEV_MODEL": "router-test-model",
        "DOVIDEO_JEV_API_KEY": "unit-test-key",
        "DOVIDEO_FAST_MODEL": "deepseek-flash",
        "DOVIDEO_FAST_REASONING_EFFORT": "none",
        "DOVIDEO_BALANCED_MODEL": "deepseek-flash",
        "DOVIDEO_DEEP_MODEL": "deepseek-v4-pro",
        "DOVIDEO_DEEP_REASONING_EFFORT": "max",
        "DOVIDEO_DEEP_MAX_TOKENS": "65536",
    }
    settings = ModelRoutingProductionSettings.from_environment(
        values,
        balanced_model="unused-fallback-model",
    )

    assert settings.enabled is True
    assert settings.model_for(ModelRouteLane.FAST) == "deepseek-flash"
    assert settings.model_for(ModelRouteLane.BALANCED) == "deepseek-flash"
    assert settings.model_for(ModelRouteLane.DEEP) == "deepseek-v4-pro"
    assert settings.request_settings_for(ModelRouteLane.FAST).request_fields() == {
        "reasoning_effort": "none"
    }
    assert settings.request_settings_for(ModelRouteLane.BALANCED).request_fields() == {}
    assert settings.request_settings_for(ModelRouteLane.DEEP).request_fields() == {
        "reasoning_effort": "max",
        "max_tokens": 65_536,
    }


@pytest.mark.asyncio
async def test_chat_request_omits_unconfigured_profile_fields_and_forwards_configured_ones() -> None:
    base = ProviderConfig(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
    )
    cases = (
        (ModelRequestSettings(), {}),
        (ModelRequestSettings(reasoning_effort="none"), {"reasoning_effort": "none"}),
        (
            ModelRequestSettings(reasoning_effort="max", max_tokens=65_536),
            {"reasoning_effort": "max", "max_tokens": 65_536},
        ),
    )
    for request_settings, expected_fields in cases:
        http = _CapturingHttpClient()
        client = OpenAICompatibleChatClient(
            base,
            request_settings=request_settings,
            client=http,
        )
        await client.complete(({"role": "user", "content": "Return JSON."},))
        payload = http.calls[0]["json"]
        assert isinstance(payload, dict)
        assert {
            key: payload[key]
            for key in ("reasoning_effort", "max_tokens")
            if key in payload
        } == expected_fields
        assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_openrouter_transport_reuses_jev_key_and_pins_execution_provider() -> None:
    config = ProviderConfig.from_environment({
        "DOVIDEO_MODEL_TRANSPORT": "openrouter",
        "DOVIDEO_MODEL_PROVIDER_TAG": "nextbit/fp8",
        "DOVIDEO_OPENROUTER_API_KEY": "shared-secret",
        "DOVIDEO_BALANCED_MODEL": "deepseek/deepseek-v4.1-flash",
        "DOVIDEO_MODEL_BASE_URL": "https://api.deepseek.com",
        "DOVIDEO_MODEL_API_KEY": "old-direct-secret",
    }, required=True)
    assert config is not None
    assert config.api_key == "shared-secret"
    assert config.chat_url == "https://openrouter.ai/api/v1/chat/completions"
    assert config.provider_only == ("nextbit/fp8",)
    assert config.provider_data_collection == "deny"
    assert config.provider_zdr is True
    settings = ModelRoutingProductionSettings(
        enabled=True,
        fast_model=config.model,
        balanced_model=config.model,
        deep_model="deepseek/deepseek-v4-pro-0813",
        fast_reasoning_effort="none",
        deep_reasoning_effort="max",
        deep_max_tokens=65_536,
    )
    for lane in (ModelRouteLane.FAST, ModelRouteLane.BALANCED, ModelRouteLane.DEEP):
        http = _CapturingHttpClient()
        lane_config = settings.provider_config_for(config, lane)
        assert lane_config is not None
        client = OpenAICompatibleChatClient(
            lane_config,
            request_settings=settings.request_settings_for(lane),
            client=http,
        )
        await client.complete(({"role": "user", "content": "Return JSON."},))
        payload = http.calls[0]["json"]
        assert payload["provider"] == {
            "only": ["nextbit/fp8"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }
        if lane is ModelRouteLane.FAST:
            assert payload["reasoning_effort"] == "none"
        elif lane is ModelRouteLane.BALANCED:
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == "max"
            assert payload["max_tokens"] == 65_536
        identity = settings.effective_profile_identity(lane, provider_config=lane_config)
        assert identity.as_dict()["transport"] == "openrouter"
        assert identity.as_dict()["providerOnly"] == ["nextbit/fp8"]
        assert identity.as_dict()["providerDataCollection"] == "deny"
        assert identity.as_dict()["providerZdr"] is True
        assert identity.fingerprint != settings.effective_profile_identity(lane).fingerprint


def test_openrouter_transport_never_falls_back_to_direct_credential() -> None:
    with pytest.raises(ProviderConfigurationError, match="OpenRouter API key is required"):
        ProviderConfig.from_environment({
            "DOVIDEO_MODEL_TRANSPORT": "openrouter",
            "DOVIDEO_MODEL_PROVIDER_TAG": "nextbit/fp8",
            "DOVIDEO_MODEL_API_KEY": "direct-only-secret",
            "DOVIDEO_MODEL_BASE_URL": "https://api.deepseek.com",
        }, required=True)


def test_missing_effort_preserves_balanced_request_defaults() -> None:
    assert ModelRequestSettings().request_fields() == {}
    assert ModelRequestSettings(max_tokens=65_536).request_fields() == {
        "max_tokens": 65_536
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"reasoning_effort": "extreme"},
        {"reasoning_effort": "MAX"},
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_tokens": 393_217},
    ],
)
def test_invalid_model_request_settings_are_rejected(settings) -> None:
    with pytest.raises(ProviderConfigurationError):
        ModelRequestSettings(**settings)


def test_jev_suggestion_cannot_supply_provider_execution_settings() -> None:
    with pytest.raises(ValidationError):
        RoutingSuggestion.model_validate(
            {
                "suggestedLane": "FAST",
                "confidence": 0.9,
                "reasoning_effort": "none",
                "model": "deepseek-flash",
            }
        )

    context = TaskRoutingContext(
        taskKey=TaskKey(1, "synthetic route request", AnalysisMode.GENERAL),
        mode=AnalysisMode.GENERAL,
        userGoal="synthetic route request",
    )
    payload = JevModelRouter.request_payload(context, model="router-test-model")
    assert set(payload["questions"]) == {"model_lane"}
    assert "reasoning_effort" not in payload
    assert "deepseek-flash" not in repr(payload)

from __future__ import annotations

import math

import pytest

from dovideo.application import (
    ModelRouteLane,
    ModelRoutingPolicy,
    ModelRoutingService,
    RoutingSuggestion,
    TaskRoutingContext,
)
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AnalysisMode
from dovideo.infrastructure.providers import (
    JEV_DEFAULT_ENDPOINT,
    OPENROUTER_DECISIONS_ENDPOINT,
    OPENROUTER_JEV_MODEL,
    JevConfigurationError,
    JevModelRouter,
    JevResponseError,
    JevRouterError,
    JevRouterSettings,
    JevRouterUnavailableError,
    JevTransport,
    ProviderHttpResponse,
)
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry


def _context() -> TaskRoutingContext:
    return TaskRoutingContext(
        taskKey=TaskKey(71001, "Describe the main event in a short synthetic clip", AnalysisMode.GENERAL),
        mode=AnalysisMode.GENERAL,
        userGoal="Describe the main event in a short synthetic clip",
        mediaDurationMs=30_000,
        segmentCount=1,
        chunkCount=1,
        asrAvailable=True,
        ocrAvailable=False,
    )


class _Http:
    def __init__(
        self,
        body=None,
        *,
        status: int = 200,
        error: BaseException | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.error = error
        self.calls: list[tuple[str, dict, dict, float]] = []

    async def post(self, url, *, headers, json, timeout):
        self.calls.append((url, dict(headers), json, timeout))
        if self.error is not None:
            raise self.error
        return ProviderHttpResponse(status_code=self.status, body=self.body)


class _Observer:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record_jev_routing(self, **values) -> None:
        self.records.append(dict(values))


def _openrouter_settings(environ: dict[str, str] | None = None) -> JevRouterSettings:
    values = {
        "DOVIDEO_JEV_TRANSPORT": "openrouter",
        "DOVIDEO_OPENROUTER_API_KEY": "openrouter-unit-secret",
    }
    if environ:
        values.update(environ)
    return JevRouterSettings.from_environment(values, required=True)


def _openrouter_router(
    http: _Http,
    *,
    observer: _Observer | None = None,
) -> JevModelRouter:
    return JevModelRouter(
        _openrouter_settings(),
        client=http,
        observer=observer,
    )


def _answer(lane: str = "BALANCED", confidence: float = 0.95) -> dict:
    return {
        "answers": {
            "model_lane": {
                "type": "choice",
                "choice": lane,
                "confidence": confidence,
                "probabilities": {"FAST": 1.0},
            }
        }
    }


def test_transport_defaults_to_typesafe_direct_and_preserves_legacy_credential() -> None:
    settings = JevRouterSettings.from_environment(
        {
            "DOVIDEO_JEV_ENDPOINT": "https://direct.invalid/v1/systemone",
            "DOVIDEO_JEV_MODEL": "direct-model-v1",
            "DOVIDEO_JEV_API_KEY": "direct-unit-secret",
            "DOVIDEO_OPENROUTER_API_KEY": "openrouter-unit-secret",
        },
        required=True,
    )
    assert settings.transport is JevTransport.TYPESAFE_DIRECT
    assert settings.gateway == "TYPESAFE_DIRECT"
    assert settings.endpoint == "https://direct.invalid/v1/systemone"
    assert settings.model == "direct-model-v1"
    assert settings.active_api_key == "direct-unit-secret"
    assert settings.openrouter_api_key is None
    assert "direct-unit-secret" not in repr(settings)
    assert "openrouter-unit-secret" not in repr(settings)


def test_openrouter_selects_fixed_endpoint_pinned_model_and_separate_credential() -> None:
    settings = _openrouter_settings(
        {
            "DOVIDEO_JEV_TRANSPORT": "OPENROUTER",
            "DOVIDEO_JEV_ENDPOINT": "https://direct.invalid/v1/systemone",
            "DOVIDEO_JEV_API_KEY": "direct-unit-secret",
        }
    )
    assert settings.transport is JevTransport.OPENROUTER
    assert settings.gateway == "OPENROUTER"
    assert settings.endpoint == OPENROUTER_DECISIONS_ENDPOINT
    assert settings.model == OPENROUTER_JEV_MODEL
    assert settings.active_api_key == "openrouter-unit-secret"
    assert settings.api_key is None
    assert "openrouter-unit-secret" not in repr(settings)
    assert "direct-unit-secret" not in repr(settings)


@pytest.mark.asyncio
async def test_openrouter_requires_its_own_key_and_does_not_reuse_direct_key() -> None:
    settings = JevRouterSettings.from_environment(
        {
            "DOVIDEO_JEV_TRANSPORT": "openrouter",
            "DOVIDEO_JEV_API_KEY": "direct-unit-secret",
        }
    )
    http = _Http(_answer())
    router = JevModelRouter(settings, client=http)

    with pytest.raises(JevConfigurationError, match="OpenRouter API key"):
        # The adapter validates before issuing HTTP.
        await router.route(_context())

    assert http.calls == []


def test_openrouter_model_is_pinned_and_aliases_are_rejected() -> None:
    with pytest.raises(JevConfigurationError, match="pinned benchmark identity"):
        _openrouter_settings({"DOVIDEO_JEV_MODEL": "~typesafe/jev-latest"})
    with pytest.raises(JevConfigurationError, match="pinned benchmark identity"):
        _openrouter_settings({"DOVIDEO_JEV_MODEL": "typesafe/jev-1.12"})


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(JevConfigurationError, match="Jev transport"):
        JevRouterSettings.from_environment({"DOVIDEO_JEV_TRANSPORT": "unknown"})


@pytest.mark.asyncio
async def test_openrouter_posts_pinned_single_choice_and_only_approved_state() -> None:
    http = _Http(_answer("DEEP", 0.94))
    suggestion = await _openrouter_router(http).route(_context())

    assert suggestion.suggested_lane is ModelRouteLane.DEEP
    assert suggestion.confidence == 0.94
    assert not hasattr(suggestion, "model")
    assert not hasattr(suggestion, "provider")
    assert len(http.calls) == 1
    url, headers, payload, _timeout = http.calls[0]
    assert url == OPENROUTER_DECISIONS_ENDPOINT
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "Bearer openrouter-unit-secret"
    assert payload == {
        "model": OPENROUTER_JEV_MODEL,
        "state": {
            "userGoal": "Describe the main event in a short synthetic clip",
            "mode": "GENERAL",
            "mediaDurationMs": 30_000,
            "segmentCount": 1,
            "chunkCount": 1,
            "asrAvailable": True,
            "ocrAvailable": False,
        },
        "questions": {
            "model_lane": {
                "type": "choice",
                "instructions": (
                    "Choose exactly one analysis lane. Return only a choice answer. "
                    "The lane is a logical DOVideo hint, not a provider or model name."
                ),
                "criteria": {
                    "FAST": "Use for straightforward analysis with limited reasoning or cross-video synthesis.",
                    "BALANCED": "Use for ordinary multi-step video analysis with moderate reasoning complexity.",
                    "DEEP": (
                        "Use for substantial multi-step reasoning, long-range comparison, "
                        "complex synthesis, or difficult constraint satisfaction."
                    ),
                },
            }
        },
    }
    serialized = repr(payload)
    for forbidden in (
        "mediaId",
        "userId",
        "transcript",
        "ocr_body",
        "ToolResult",
        "goldEvidence",
        "requiredFacts",
        "referenceAnswer",
        "datasetAnnotation",
        "openrouter-unit-secret",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"answers": {}},
        {"answers": {"model_lane": {"type": "noul", "choice": "FAST", "confidence": 0.9}}},
        {"answers": {"model_lane": {"type": "choice", "choice": "provider-model", "confidence": 0.9}}},
        {"answers": {"model_lane": {"type": "choice", "choice": "FAST", "confidence": float("nan")}}},
        {"answers": {"model_lane": {"type": "choice", "choice": "FAST", "confidence": float("inf")}}},
        {"answers": {"model_lane": {"type": "choice", "choice": "FAST", "confidence": True}}},
    ],
)
def test_malformed_or_out_of_contract_openrouter_answer_is_rejected(body) -> None:
    with pytest.raises(JevResponseError):
        JevModelRouter.parse_response(body)


@pytest.mark.asyncio
async def test_openrouter_usage_and_gateway_metadata_are_bounded_and_observed() -> None:
    observer = _Observer()
    http = _Http(
        {
            **_answer("FAST", 0.97),
            "model": "typesafe/jev-1.13-20260917",
            "provider": "untrusted-response-provider",
            "usage": {
                "input_tokens": 147,
                "output_tokens": 29,
                "cost": 0.000006174,
            },
        }
    )
    await _openrouter_router(http, observer=observer).route(_context())

    assert len(observer.records) == 1
    record = observer.records[0]
    assert record["gateway"] == "OPENROUTER"
    assert record["decision_model"] == OPENROUTER_JEV_MODEL
    assert record["router_model"] == "typesafe/jev-1.13-20260917"
    assert record["input_tokens"] == 147
    assert record["output_tokens"] == 29
    assert math.isclose(record["usage_cost_usd"], 0.000006174)
    assert "untrusted-response-provider" not in repr(record)


@pytest.mark.asyncio
async def test_invalid_usage_is_marked_absent_without_invalidating_valid_choice() -> None:
    observer = _Observer()
    http = _Http({**_answer(), "usage": {"input_tokens": -1, "cost": float("nan")}})
    suggestion = await _openrouter_router(http, observer=observer).route(_context())
    assert suggestion.suggested_lane is ModelRouteLane.BALANCED
    assert observer.records[0]["input_tokens"] is None
    assert observer.records[0]["usage_cost_usd"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, "ROUTER_ERROR"),
        (401, "ROUTER_ERROR"),
        (403, "ROUTER_ERROR"),
        (429, "ROUTER_UNAVAILABLE"),
        (500, "ROUTER_UNAVAILABLE"),
        (502, "ROUTER_UNAVAILABLE"),
        (503, "ROUTER_UNAVAILABLE"),
        (524, "ROUTER_UNAVAILABLE"),
    ],
)
async def test_openrouter_failures_keep_existing_j1_fallback_classification(status, reason) -> None:
    http = _Http({"error": {"message": "private gateway details"}}, status=status)
    router = _openrouter_router(http)
    service = ModelRoutingService(
        router,
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )

    decision = await service.route(_context())

    assert decision.lane is ModelRouteLane.BALANCED
    assert decision.fallback_used is True
    assert decision.reason_code.value == reason
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_openrouter_transport_timeout_uses_existing_router_unavailable_mapping() -> None:
    http = _Http(error=TimeoutError("private network details"))
    router = _openrouter_router(http)
    with pytest.raises(JevRouterUnavailableError):
        await router.route(_context())
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_openrouter_network_error_uses_existing_router_unavailable_mapping() -> None:
    http = _Http(error=OSError("private network details"))
    router = _openrouter_router(http)
    with pytest.raises(JevRouterUnavailableError):
        await router.route(_context())
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_jev_smoke_fallback_is_not_a_connectivity_pass() -> None:
    http = _Http(_answer("DEEP", 0.4))
    service = ModelRoutingService(
        _openrouter_router(http),
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )

    decision = await service.route(_context())
    connectivity_passed = (
        not decision.fallback_used
        and decision.reason_code.value == "ROUTER_ACCEPTED"
    )

    assert decision.lane is ModelRouteLane.BALANCED
    assert decision.reason_code.value == "LOW_CONFIDENCE"
    assert decision.fallback_used is True
    assert connectivity_passed is False


@pytest.mark.asyncio
async def test_openrouter_failure_fallback_is_not_a_connectivity_pass() -> None:
    http = _Http({}, status=401)
    service = ModelRoutingService(
        _openrouter_router(http),
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )
    decision = await service.route(_context())
    assert decision.fallback_used is True
    assert decision.reason_code.value == "ROUTER_ERROR"
    assert decision.reason_code.value != "ROUTER_ACCEPTED"


def test_direct_settings_constructor_remains_backward_compatible() -> None:
    settings = JevRouterSettings(
        endpoint=JEV_DEFAULT_ENDPOINT,
        model="legacy-direct-model",
        api_key="legacy-direct-secret",
    )
    assert settings.transport is JevTransport.TYPESAFE_DIRECT
    assert settings.endpoint == JEV_DEFAULT_ENDPOINT
    assert settings.model == "legacy-direct-model"
    assert settings.active_api_key == "legacy-direct-secret"


def test_r4_telemetry_persists_gateway_and_reported_cost_without_raw_response() -> None:
    class _TraceStore:
        def __init__(self) -> None:
            self.structural: list[dict] = []

        def latest(self, _key):
            return {}

        def start(self, _key):
            return "trace"

        def record_structural_for_key(self, _key, value):
            self.structural.append(dict(value))

    store = _TraceStore()
    telemetry = R4AgentTelemetry(store)  # type: ignore[arg-type]
    token = telemetry.bind(TaskKey(71002, "synthetic", AnalysisMode.GENERAL))
    try:
        telemetry.record_jev_routing(
            status_code=200,
            latency_ms=22.5,
            gateway="OPENROUTER",
            decision_model=OPENROUTER_JEV_MODEL,
            router_model="typesafe/jev-1.13-20260917",
            input_tokens=123,
            output_tokens=14,
            usage_cost_usd=0.000005166,
            fallback=False,
        )
    finally:
        telemetry.reset(token)

    assert store.structural == [
        {
            "kind": "jevRoutingTransport",
            "statusCode": 200,
            "latencyMs": 22.5,
            "gateway": "OPENROUTER",
            "decisionModel": OPENROUTER_JEV_MODEL,
            "routerModel": "typesafe/jev-1.13-20260917",
            "inputTokens": 123,
            "outputTokens": 14,
            "usageCostUsd": 0.000005166,
            "fallback": False,
        }
    ]

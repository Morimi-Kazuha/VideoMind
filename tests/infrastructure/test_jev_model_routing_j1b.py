from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application import (
    ModelRouteLane,
    ModelRoutingPolicy,
    ModelRoutingService,
    RoutingSuggestion,
    TaskRoutingContext,
)
from dovideo.application.value_objects import TaskKey
from dovideo.domain import AgentPlan, AgentState, AnalysisMode, ModeProfile, VideoContext, VideoSegment
from dovideo.infrastructure.model_routing import (
    ModelRoutingProductionSettings,
    ProductionModelRoutingAgentLoop,
    RoutingProfileUnavailableError,
    build_task_routing_context,
)
from dovideo.infrastructure.providers import (
    JevModelRouter,
    JevRouterError,
    JevRouterSettings,
    ProviderConfig,
    ProviderHttpResponse,
)
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry, create_r4_provider_stack
from dovideo.infrastructure.x1_config import X1ToolCallingSettings


def _routing_context() -> TaskRoutingContext:
    return TaskRoutingContext(
        taskKey=TaskKey(7, "explain the opening", AnalysisMode.REVIEW),
        mode=AnalysisMode.REVIEW,
        userGoal="explain the opening",
        mediaDurationMs=120_000,
        segmentCount=2,
        chunkCount=1,
        asrAvailable=True,
        ocrAvailable=True,
    )


class _Http:
    def __init__(self, body=None, *, status=200, error: BaseException | None = None) -> None:
        self.body = body
        self.status = status
        self.error = error
        self.calls: list[tuple[str, dict, dict, float]] = []

    async def post(self, url, *, headers, json, timeout):
        self.calls.append((url, dict(headers), json, timeout))
        if self.error is not None:
            raise self.error
        return ProviderHttpResponse(status_code=self.status, body=self.body)


def _router(http: _Http, *, attempts: int = 1) -> JevModelRouter:
    return JevModelRouter(
        JevRouterSettings(
            endpoint="https://jev.invalid/v1/systemone",
            model="jev-test-model",
            api_key="unit-secret",
            timeout_seconds=0.2,
            max_attempts=attempts,
        ),
        client=http,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["FAST", "BALANCED", "DEEP"])
async def test_jev_uses_official_single_choice_request_and_maps_confidence(lane: str) -> None:
    http = _Http(
        {
            "model": "jev-response-model",
            "usage": {"input_tokens": 11, "output_tokens": 3},
            "answers": {
                "model_lane": {
                    "type": "choice",
                    "choice": lane,
                    "confidence": 0.95,
                    "probabilities": {"FAST": 0.1, "DEEP": 0.9},
                }
            },
        }
    )
    suggestion = await _router(http).route(_routing_context())
    assert suggestion.suggested_lane.value == lane
    assert suggestion.confidence == 0.95
    assert len(http.calls) == 1
    url, headers, payload, timeout = http.calls[0]
    assert url.endswith("/v1/systemone")
    assert headers["Authorization"] == "Bearer unit-secret"
    assert timeout == 0.2
    assert payload["model"] == "jev-test-model"
    assert set(payload["questions"]) == {"model_lane"}
    question = payload["questions"]["model_lane"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"FAST", "BALANCED", "DEEP"}
    assert payload["state"] == {
        "userGoal": "explain the opening",
        "mode": "REVIEW",
        "mediaDurationMs": 120_000,
        "segmentCount": 2,
        "chunkCount": 1,
        "asrAvailable": True,
        "ocrAvailable": True,
    }
    serialized = repr(payload)
    assert "transcript" not in serialized
    assert "unit-secret" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"answers": {}},
        {"answers": {"model_lane": {"type": "text", "choice": "FAST", "confidence": 0.9}}},
        {"answers": {"model_lane": {"type": "choice", "choice": "GPT", "confidence": 0.9}}},
        {"answers": {"model_lane": {"type": "choice", "choice": "FAST", "confidence": float("nan")}}},
    ],
)
async def test_invalid_jev_shape_becomes_invalid_suggestion_fallback(body) -> None:
    http = _Http(body)
    service = ModelRoutingService(
        _router(http),
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )
    decision = await service.route(_routing_context())
    assert decision.lane is ModelRouteLane.BALANCED
    assert decision.reason_code.value == "INVALID_SUGGESTION"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_jev_http_failure_falls_back_without_analysis_failure(status: int) -> None:
    http = _Http({}, status=status)
    service = ModelRoutingService(
        _router(http),
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )
    decision = await service.route(_routing_context())
    assert decision.lane is ModelRouteLane.BALANCED
    assert decision.reason_code.value in {"ROUTER_ERROR", "ROUTER_UNAVAILABLE"}


@pytest.mark.asyncio
async def test_jev_timeout_is_unavailable_and_probabilities_do_not_override_choice() -> None:
    http = _Http(error=TimeoutError())
    service = ModelRoutingService(
        _router(http),
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )
    decision = await service.route(_routing_context())
    assert decision.reason_code.value == "ROUTER_UNAVAILABLE"
    assert decision.lane is ModelRouteLane.BALANCED

    accepted_http = _Http(
        {
            "answers": {
                "model_lane": {
                    "type": "choice",
                    "choice": "FAST",
                    "confidence": 0.91,
                    "probabilities": {"DEEP": 0.99},
                }
            }
        }
    )
    accepted = await ModelRoutingService(
        _router(accepted_http),
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    ).route(_routing_context())
    assert accepted.lane is ModelRouteLane.FAST


def test_jev_configuration_is_secret_safe_and_feature_defaults_off() -> None:
    settings = JevRouterSettings.from_environment({})
    assert settings.model == ""
    assert "unit-secret" not in repr(
        JevRouterSettings(model="m", api_key="unit-secret")
    )
    with pytest.raises(ValueError):
        JevRouterSettings.from_environment(
            {"DOVIDEO_JEV_MODEL": "m"},
            required=True,
        )
    with pytest.raises(ValueError):
        ModelRoutingProductionSettings.from_environment(
            {
                "DOVIDEO_MODEL_ROUTING_ENABLED": "true",
                "DOVIDEO_JEV_MODEL": "m",
            },
            balanced_model="balanced",
        )


def test_production_settings_disable_missing_fast_and_deep_without_late_failure() -> None:
    settings = ModelRoutingProductionSettings.from_environment(
        {
            "DOVIDEO_MODEL_ROUTING_ENABLED": "true",
            "DOVIDEO_JEV_MODEL": "jev",
            "DOVIDEO_JEV_API_KEY": "unit-secret",
        },
        balanced_model="current-model",
    )
    assert settings.enabled is True
    assert settings.enabled_lanes == frozenset({ModelRouteLane.BALANCED})
    assert settings.policy_configuration().fast_enabled is False
    assert settings.policy_configuration().deep_enabled is False
    assert settings.model_for(ModelRouteLane.BALANCED) == "current-model"


def test_build_routing_context_projects_only_bounded_signals_and_concrete_mode() -> None:
    context = VideoContext(
        source="memory://video",
        user_goal="compare the opening",
        segments=(
            VideoSegment(
                startMs=0,
                endMs=60_000,
                transcript="private transcript body",
                ocrTexts=("private OCR body",),
            ),
        ),
    )
    routing = build_task_routing_context(
        context,
        media_id=3,
        profile=ModeProfile(mode=AnalysisMode.CREATION),
    )
    assert routing.mode is AnalysisMode.CREATION
    assert routing.media_id == 3
    assert routing.asr_available is True
    assert routing.ocr_available is True
    payload = JevModelRouter.request_payload(routing, model="jev")
    assert "private transcript body" not in repr(payload)
    assert "private OCR body" not in repr(payload)
    assert payload["state"]["mode"] == "CREATION"


class _RouteStore:
    def __init__(self) -> None:
        self.values = {}
        self.saves = 0

    async def load_model_routing(self, key):
        return self.values.get(key)

    async def save_model_routing(self, key, decision):
        self.saves += 1
        self.values[key] = decision


class _Router:
    def __init__(self, lane: str = "FAST") -> None:
        self.lane = lane
        self.calls = 0

    async def route(self, context):
        self.calls += 1
        assert context.mode is AnalysisMode.GENERAL
        return RoutingSuggestion(suggestedLane=self.lane, confidence=0.95)


class _LaneLoop:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, context, *, media_id=None, profile=None):
        del context, media_id, profile
        self.calls += 1
        return AgentState(
            goal="explain",
            plan=AgentPlan(understoodGoal="explain", tasks=("task",)),
            round=1,
        )


def _production_wrapper(lane: str = "FAST"):
    router = _Router(lane)
    service = ModelRoutingService(
        router,
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )
    store = _RouteStore()
    loops = {lane_name: _LaneLoop() for lane_name in ModelRouteLane}
    wrapper = ProductionModelRoutingAgentLoop(
        loops,
        service,
        checkpoint=store,
    )
    context = VideoContext(
        source="memory://video",
        user_goal="explain",
        segments=(VideoSegment(startMs=0, endMs=10_000, transcript="opening"),),
    )
    return wrapper, router, store, loops, context


@pytest.mark.asyncio
async def test_production_routes_once_and_reuses_durable_decision_after_recomposition() -> None:
    wrapper, router, store, loops, context = _production_wrapper()
    await wrapper.run(context, media_id=7, profile=ModeProfile(mode=AnalysisMode.GENERAL))
    assert router.calls == 1
    assert loops[ModelRouteLane.FAST].calls == 1
    assert store.saves == 1

    recovered_wrapper, recovered_router, recovered_loops, _unused, _context = (
        wrapper,
        router,
        loops,
        store,
        context,
    )
    # Recompose the wrapper with a router that would choose a different lane;
    # the durable decision is authoritative and the router is not called.
    second_router = _Router("DEEP")
    second_service = ModelRoutingService(
        second_router,
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )
    second_loops = {lane_name: _LaneLoop() for lane_name in ModelRouteLane}
    second = ProductionModelRoutingAgentLoop(
        second_loops,
        second_service,
        checkpoint=store,
    )
    await second.run(context, media_id=7, profile=ModeProfile(mode=AnalysisMode.GENERAL))
    assert second_router.calls == 0
    assert second_loops[ModelRouteLane.FAST].calls == 1
    assert second_loops[ModelRouteLane.DEEP].calls == 0
    del recovered_wrapper, recovered_router, recovered_loops


@pytest.mark.asyncio
async def test_disabled_production_routing_makes_zero_router_calls_and_uses_balanced() -> None:
    router = _Router("FAST")
    service = ModelRoutingService(
        router,
        policy=ModelRoutingPolicy(adaptive_routing_enabled=False),
    )
    store = _RouteStore()
    loops = {lane_name: _LaneLoop() for lane_name in ModelRouteLane}
    wrapper = ProductionModelRoutingAgentLoop(loops, service, checkpoint=store)
    context = VideoContext(
        source="memory://video",
        user_goal="explain",
        segments=(VideoSegment(startMs=0, endMs=10_000, transcript="opening"),),
    )
    await wrapper.run(context, media_id=7, profile=ModeProfile(mode=AnalysisMode.GENERAL))
    assert router.calls == 0
    assert loops[ModelRouteLane.BALANCED].calls == 1
    assert store.saves == 0


@pytest.mark.asyncio
async def test_low_confidence_jev_suggestion_falls_back_to_balanced_lane() -> None:
    class _LowConfidenceRouter:
        async def route(self, context):
            del context
            return RoutingSuggestion(suggestedLane="DEEP", confidence=0.4)

    service = ModelRoutingService(
        _LowConfidenceRouter(),
        policy=ModelRoutingPolicy(adaptive_routing_enabled=True),
    )
    store = _RouteStore()
    loops = {lane_name: _LaneLoop() for lane_name in ModelRouteLane}
    wrapper = ProductionModelRoutingAgentLoop(loops, service, checkpoint=store)
    context = VideoContext(
        source="memory://video",
        user_goal="explain",
        segments=(VideoSegment(startMs=0, endMs=10_000, transcript="opening"),),
    )
    await wrapper.run(context, media_id=7, profile=ModeProfile(mode=AnalysisMode.GENERAL))
    assert loops[ModelRouteLane.BALANCED].calls == 1
    assert loops[ModelRouteLane.DEEP].calls == 0


@pytest.mark.asyncio
async def test_existing_route_to_removed_profile_fails_closed_instead_of_rerouting() -> None:
    router = _Router("FAST")
    service = ModelRoutingService(
        router,
        policy=ModelRoutingPolicy(adaptive_routing_enabled=False),
    )
    store = _RouteStore()
    key = TaskKey(7, "explain", AnalysisMode.GENERAL)
    store.values[key] = service.policy.fallback(
        reason_code="ROUTING_DISABLED",
    ).model_copy(update={"lane": ModelRouteLane.FAST, "fallback_used": False})
    loops = {ModelRouteLane.BALANCED: _LaneLoop()}
    wrapper = ProductionModelRoutingAgentLoop(loops, service, checkpoint=store)
    context = VideoContext(
        source="memory://video",
        user_goal="explain",
        segments=(VideoSegment(startMs=0, endMs=10_000, transcript="opening"),),
    )
    with pytest.raises(RoutingProfileUnavailableError):
        await wrapper.run(context, media_id=7, profile=ModeProfile(mode=AnalysisMode.GENERAL))
    assert router.calls == 0


def test_provider_profiles_are_infrastructure_only() -> None:
    settings = ModelRoutingProductionSettings(
        enabled=True,
        fast_model="fast-model",
        balanced_model="balanced-model",
        deep_model="deep-model",
        jev=JevRouterSettings(model="jev", api_key="unit-secret"),
    )
    base = ProviderConfig(
        base_url="https://provider.invalid/v1",
        model="current-model",
        api_key="provider-secret",
    )
    fast = settings.provider_config_for(base, ModelRouteLane.FAST)
    assert fast is not None and fast.model == "fast-model"
    assert not hasattr(RoutingSuggestion(suggestedLane="FAST", confidence=0.9), "model")
    assert "provider-secret" not in repr(fast)


class _TraceStore:
    def start(self, key):
        del key
        return "trace"

    def latest(self, key):
        del key
        return {}

    def increment_for_key(self, key, metric, amount=1):
        del key, metric, amount

    def observe_for_key(self, key, metric, value):
        del key, metric, value

    def record_structural_for_key(self, key, diagnostic):
        del key, diagnostic

    def add_usage_for_key(self, key, estimated_tokens=0, estimated_cost=0.0, usage=None):
        del key, estimated_tokens, estimated_cost, usage
        return {"estimatedTokens": 0, "estimatedCost": 0.0}

    def current_usage_for_key(self, key):
        del key
        return {"estimatedTokens": 0, "estimatedCost": 0.0}


class _RoutingCheckpoint:
    async def load_model_routing(self, key):
        del key
        return None

    async def save_model_routing(self, key, decision):
        del key, decision


def test_r4_stack_wires_three_configured_model_profiles_without_network(monkeypatch) -> None:
    import dovideo.infrastructure.r4_runtime as runtime

    monkeypatch.setattr(
        runtime.ProviderConfig,
        "from_environment",
        classmethod(
            lambda cls, environ=None, *, prefix="DOVIDEO_", required=False: ProviderConfig(
                base_url="https://provider.invalid/v1",
                model="current-model",
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "embedding_provider_config_from_environment",
        lambda environ=None, *, required=True: ProviderConfig(
            base_url="https://embedding.invalid/v1",
            model="BAAI/bge-m3",
            embedding_model="BAAI/bge-m3",
        ),
    )
    settings = ModelRoutingProductionSettings(
        enabled=True,
        fast_model="deepseek-flash",
        balanced_model="deepseek-flash",
        deep_model="deepseek-v4-pro",
        fast_reasoning_effort="none",
        deep_reasoning_effort="max",
        deep_max_tokens=65_536,
        jev=JevRouterSettings(model="jev-model", api_key="unit-secret"),
    )
    stack = create_r4_provider_stack(
        _RoutingCheckpoint(),
        object(),
        R4AgentTelemetry(_TraceStore()),
        None,
        tool_settings=X1ToolCallingSettings(enabled=False),
        routing_settings=settings,
    )
    try:
        assert isinstance(stack.agent_loop, ProductionModelRoutingAgentLoop)
        assert stack.routing_enabled is True
        assert stack.routing_service is not None
        assert set(stack.model_adapters) == set(ModelRouteLane)
        for lane, expected in {
            ModelRouteLane.FAST: "deepseek-flash",
            ModelRouteLane.BALANCED: "deepseek-flash",
            ModelRouteLane.DEEP: "deepseek-v4-pro",
        }.items():
            chat = stack.model_adapters[lane].planner._chat
            assert chat.inner.config.model == expected
            assert stack.model_adapters[lane].executor._chat is chat
            assert stack.model_adapters[lane].critic._chat is chat

        balanced_request = stack.chat_client.request_settings
        assert balanced_request.request_fields() == {}
        fast_client = stack.model_adapters[ModelRouteLane.FAST].planner._chat.inner
        assert fast_client.request_settings.request_fields() == {
            "reasoning_effort": "none"
        }
        deep_client = stack.model_adapters[ModelRouteLane.DEEP].planner._chat.inner
        assert deep_client.request_settings.request_fields() == {
            "reasoning_effort": "max",
            "max_tokens": 65_536,
        }
        fast_identity = stack.effective_model_profiles[ModelRouteLane.FAST]
        balanced_identity = stack.effective_model_profiles[ModelRouteLane.BALANCED]
        deep_identity = stack.effective_model_profiles[ModelRouteLane.DEEP]
        assert fast_identity.profile_id == "fast-profile"
        assert balanced_identity.profile_id == "balanced-profile"
        assert fast_identity.resolved_model_id == balanced_identity.resolved_model_id
        assert fast_identity.fingerprint != balanced_identity.fingerprint
        assert deep_identity.fingerprint not in {
            fast_identity.fingerprint,
            balanced_identity.fingerprint,
        }
    finally:
        import asyncio

        asyncio.run(stack.close())

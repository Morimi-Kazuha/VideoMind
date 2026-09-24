from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from dovideo.application import (
    DEFAULT_ROUTING_CONFIDENCE_THRESHOLD,
    InvalidRoutingContextError,
    MAX_ROUTING_CLASSIFICATION_ITEMS,
    MAX_ROUTING_GOAL_LENGTH,
    MAX_ROUTING_SEGMENT_COUNT,
    ModelProfileRegistry,
    ModelRouteLane,
    ModelRoutingConfiguration,
    ModelRoutingDecision,
    ModelRoutingPolicy,
    ModelRoutingReasonCode,
    ModelRoutingService,
    RoutingContractError,
    RoutingSuggestion,
    TaskKey,
    TaskRoutingContext,
    UnsupportedRoutingContractError,
)
from dovideo.domain import AnalysisMode


def _context(
    *,
    mode: AnalysisMode = AnalysisMode.GENERAL,
    goal: str = "inspect the video",
) -> TaskRoutingContext:
    return TaskRoutingContext(
        taskKey=TaskKey(7, goal, mode),
        mediaDurationMs=346_190,
        segmentCount=6,
        chunkCount=2,
        asrAvailable=True,
        ocrAvailable=True,
        sourceRevision="revision-1",
    )


def _enabled_config(**updates) -> ModelRoutingConfiguration:
    values = {
        "adaptiveRoutingEnabled": True,
        "confidenceThreshold": 0.70,
        "fastEnabled": True,
        "deepEnabled": True,
    }
    values.update(updates)
    return ModelRoutingConfiguration(**values)


@pytest.mark.parametrize("lane", [ModelRouteLane.FAST, ModelRouteLane.BALANCED, ModelRouteLane.DEEP])
def test_j1a_lane_contract_is_fixed_and_provider_neutral(lane: ModelRouteLane) -> None:
    suggestion = RoutingSuggestion(suggestedLane=lane, confidence=1.0)
    assert suggestion.suggested_lane is lane
    assert suggestion.routing_contract_version == "model-routing-v1"
    assert "provider" not in suggestion.model_dump(mode="json")
    assert "model" not in suggestion.model_dump(mode="json")


def test_j1a_unknown_lane_and_auto_mode_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RoutingSuggestion(suggestedLane="UNKNOWN", confidence=0.9)

    with pytest.raises(ValidationError):
        TaskRoutingContext(
            taskKey=TaskKey(7, "goal"),
            mode="AUTO",
            userGoal="goal",
        )


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_j1a_confidence_accepts_only_finite_unit_interval(confidence: float) -> None:
    assert RoutingSuggestion(suggestedLane="FAST", confidence=confidence).confidence == confidence

    with pytest.raises(ValidationError):
        RoutingSuggestion(suggestedLane="FAST", confidence=confidence - 1.1)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.nan, math.inf, -math.inf])
def test_j1a_confidence_rejects_out_of_range_nan_and_infinity(confidence: float) -> None:
    with pytest.raises(ValidationError):
        RoutingSuggestion(suggestedLane="FAST", confidence=confidence)


@pytest.mark.parametrize("lane", [ModelRouteLane.FAST, ModelRouteLane.DEEP])
def test_j1a_accepted_routes_are_policy_owned(lane: ModelRouteLane) -> None:
    policy = ModelRoutingPolicy(_enabled_config())
    decision = policy.decide(
        _context(),
        RoutingSuggestion(suggestedLane=lane, confidence=DEFAULT_ROUTING_CONFIDENCE_THRESHOLD),
    )
    assert decision.lane is lane
    assert decision.fallback_used is False
    assert decision.reason_code is ModelRoutingReasonCode.ROUTER_ACCEPTED


def test_j1a_low_confidence_and_disabled_lane_fall_back_to_balanced() -> None:
    policy = ModelRoutingPolicy(_enabled_config(deepEnabled=False))

    low = policy.decide(
        _context(),
        RoutingSuggestion(suggestedLane="FAST", confidence=0.69),
    )
    assert low.lane is ModelRouteLane.BALANCED
    assert low.confidence == 0.69
    assert low.fallback_used is True
    assert low.reason_code is ModelRoutingReasonCode.LOW_CONFIDENCE

    disabled = policy.decide(
        _context(),
        RoutingSuggestion(suggestedLane="DEEP", confidence=0.99),
    )
    assert disabled.lane is ModelRouteLane.BALANCED
    assert disabled.fallback_used is True
    assert disabled.reason_code is ModelRoutingReasonCode.LANE_DISABLED


class _FakeRouter:
    def __init__(self, response=None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[TaskRoutingContext] = []

    async def route(self, context: TaskRoutingContext):
        self.calls.append(context)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("router timeout detail"), ModelRoutingReasonCode.ROUTER_UNAVAILABLE),
        (RuntimeError("router secret detail"), ModelRoutingReasonCode.ROUTER_ERROR),
    ],
)
async def test_j1a_router_failure_is_optimization_fallback_not_task_failure(error, reason) -> None:
    router = _FakeRouter(error=error)
    service = ModelRoutingService(router, configuration=_enabled_config())

    decision = await service.route(_context())

    assert decision.lane is ModelRouteLane.BALANCED
    assert decision.fallback_used is True
    assert decision.reason_code is reason
    assert len(router.calls) == 1


@pytest.mark.asyncio
async def test_j1a_invalid_router_output_falls_back_and_disabled_mode_does_not_call_router() -> None:
    invalid_router = _FakeRouter(response={"suggestedLane": "FOURTH", "confidence": 0.99})
    invalid_service = ModelRoutingService(invalid_router, configuration=_enabled_config())
    invalid = await invalid_service.route(_context())
    assert invalid.lane is ModelRouteLane.BALANCED
    assert invalid.reason_code is ModelRoutingReasonCode.INVALID_SUGGESTION

    disabled_router = _FakeRouter(
        response=RoutingSuggestion(suggestedLane="DEEP", confidence=1.0)
    )
    disabled_service = ModelRoutingService(
        disabled_router,
        configuration=ModelRoutingConfiguration(),
    )
    disabled = await disabled_service.route(_context())
    assert disabled.lane is ModelRouteLane.BALANCED
    assert disabled.fallback_used is True
    assert disabled.reason_code is ModelRoutingReasonCode.ROUTING_DISABLED
    assert disabled_router.calls == []


@pytest.mark.asyncio
async def test_j1a_route_once_reuses_the_exact_decision_without_a_second_router_call() -> None:
    router = _FakeRouter(response={"suggestedLane": "FAST", "confidence": 0.95})
    service = ModelRoutingService(router, configuration=_enabled_config())

    first = await service.route_once(_context())
    recovered = await service.route_once(_context(), existing_decision=first)

    assert recovered == first
    assert recovered.model_dump(mode="json", by_alias=True) == first.model_dump(
        mode="json", by_alias=True
    )
    assert len(router.calls) == 1
    assert service.resolve_profile(recovered).profile_id == "fast-profile"


def test_j1a_static_profile_registry_has_exactly_three_logical_profiles() -> None:
    registry = ModelProfileRegistry.default()
    assert registry.lanes() == (
        ModelRouteLane.FAST,
        ModelRouteLane.BALANCED,
        ModelRouteLane.DEEP,
    )
    assert [profile.profile_id for profile in registry.profiles()] == [
        "fast-profile",
        "balanced-profile",
        "deep-profile",
    ]
    assert not hasattr(registry.resolve(ModelRouteLane.FAST), "provider")
    with pytest.raises(RoutingContractError):
        registry.register(ModelRouteLane.FAST, "arbitrary")


def test_j1a_context_is_bounded_and_contains_no_video_payload() -> None:
    context = _context()
    serialized = context.model_dump(mode="json", by_alias=True)
    assert serialized["segmentCount"] == 6
    assert serialized["chunkCount"] == 2
    assert "transcript" not in serialized
    assert "ocrText" not in serialized
    assert "embedding" not in serialized

    with pytest.raises(ValidationError):
        TaskRoutingContext(
            taskKey=TaskKey(7, "x" * (MAX_ROUTING_GOAL_LENGTH + 1)),
            userGoal="x" * (MAX_ROUTING_GOAL_LENGTH + 1),
        )
    with pytest.raises(ValidationError):
        TaskRoutingContext(
            taskKey=TaskKey(7, "goal"),
            segmentCount=MAX_ROUTING_SEGMENT_COUNT + 1,
        )
    with pytest.raises(ValidationError):
        RoutingSuggestion(
            suggestedLane="FAST",
            confidence=0.9,
            classificationMetadata={
                str(index): "bounded" for index in range(MAX_ROUTING_CLASSIFICATION_ITEMS + 1)
            },
        )


def test_j1a_invalid_context_fails_closed_before_policy_decision() -> None:
    policy = ModelRoutingPolicy(_enabled_config())
    with pytest.raises(InvalidRoutingContextError):
        policy.decide(
            {
                "taskKey": {"mediaId": 7, "goal": "goal", "mode": "AUTO"},
                "userGoal": "goal",
            },
            {"suggestedLane": "FAST", "confidence": 1.0},
        )


def test_j1a_reused_future_contract_is_not_silently_accepted() -> None:
    service = ModelRoutingService(
        None,
        configuration=_enabled_config(),
    )
    future = {
        "lane": "FAST",
        "confidence": 1.0,
        "fallbackUsed": False,
        "reasonCode": "ROUTER_ACCEPTED",
        "routingContractVersion": "model-routing-v2",
    }
    with pytest.raises(UnsupportedRoutingContractError):
        service.resolve_profile(future)

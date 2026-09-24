from datetime import datetime, timezone
import hashlib

import pytest

from dovideo.application import (
    AgentLoopService,
    ExecutionEventType,
    ExecutionRecordConflictError,
    ExecutionRecordService,
    EXECUTION_CONTRACT_VERSION_V1,
    EXECUTION_CONTRACT_VERSION_V2,
    HistoricalAgentReplayService,
    InMemoryExecutionRecordRepository,
    ModelProfileRegistry,
    ModelRouteLane,
    ModelRoutingDecision,
    ModelRoutingPolicy,
    ModelRoutingReasonCode,
    ModelRoutingService,
    ModelRoutingSuggestion,
    ReplayIncompleteError,
    ReplayIntegrityError,
    TaskKey,
)
from dovideo.application.mode_profiles import mode_profile_for
from dovideo.application.tool_contracts import ExecutorTurn, ExecutorTurnKind
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    PROVENANCE_VERSION,
    SourceItemIdentity,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.model_routing import (
    ModelRoutingHistoryIntegrityError,
    ProductionModelRoutingAgentLoop,
    RoutingProfileUnavailableError,
)
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry, create_r4_provider_stack
from dovideo.infrastructure.x1_config import X1ToolCallingSettings
from dovideo.infrastructure.providers import ProviderConfig
from dovideo.infrastructure.model_routing import ModelRoutingProductionSettings


REVISION = "revision-j1c"
MODE_PROFILE = mode_profile_for(AnalysisMode.GENERAL)


class _Checkpoint:
    def __init__(self):
        self.values = {}
        self.saves = 0

    async def load_model_routing(self, key):
        return self.values.get(key)

    async def save_model_routing(self, key, decision):
        self.saves += 1
        self.values[key] = decision


class _Router:
    def __init__(self, lane="FAST", confidence=0.95):
        self.lane = lane
        self.confidence = confidence
        self.calls = 0

    async def route(self, context):
        self.calls += 1
        return ModelRoutingSuggestion(
            suggestedLane=self.lane,
            confidence=self.confidence,
        )


class _Lane:
    def __init__(self):
        self.calls = 0

    async def run(self, context, *, media_id=None, profile=None):
        del media_id, profile
        self.calls += 1
        return AgentState(
            goal=context.user_goal,
            plan=AgentPlan(understoodGoal=context.user_goal, tasks=("inspect",)),
            round=1,
        )


def _context() -> VideoContext:
    return VideoContext(
        source="sha256:video-j1c",
        sourceRevision=REVISION,
        user_goal="explain the evidence",
        segments=(
            VideoSegment(
                startMs=0,
                endMs=10_000,
                transcript="opening evidence",
                sourceRevision=REVISION,
                segmentId="segment-1",
                sourceItems=(
                    SourceItemIdentity(
                        sourceItemId="item-1",
                        sourceRevision=REVISION,
                        segmentId="segment-1",
                        sourceType="ASR",
                        ordinal=0,
                        timestampMs=0,
                        contentDigest=hashlib.sha256(b"opening evidence").hexdigest(),
                        provenanceVersion=PROVENANCE_VERSION,
                    ),
                ),
            ),
        ),
    )


def _records(*, contract=EXECUTION_CONTRACT_VERSION_V2):
    repository = InMemoryExecutionRecordRepository()
    service = ExecutionRecordService(
        repository,
        id_factory=lambda: "execution-j1c",
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
        execution_contract_version=contract,
    )
    return service, repository


def _wrapper(
    records,
    checkpoint,
    router,
    *,
    enabled=True,
    lanes=None,
    resolved=None,
):
    policy = ModelRoutingPolicy(adaptive_routing_enabled=enabled)
    service = ModelRoutingService(router, policy=policy)
    selected_lanes = lanes or {
        ModelRouteLane.BALANCED,
        ModelRouteLane.FAST,
        ModelRouteLane.DEEP,
    }
    loops = {lane: _Lane() for lane in selected_lanes}
    return (
        ProductionModelRoutingAgentLoop(
            loops,
            service,
            checkpoint=checkpoint,
            execution_records=records,
            resolved_model_ids=resolved
            or {
                ModelRouteLane.BALANCED: "balanced-model-a",
                ModelRouteLane.FAST: "fast-model-a",
                ModelRouteLane.DEEP: "deep-model-a",
            },
            require_historical_route=True,
        ),
        loops,
    )


@pytest.mark.asyncio
async def test_routed_execution_records_v2_event_before_planner_and_reuses_it() -> None:
    records, repository = _records()
    checkpoint = _Checkpoint()
    router = _Router("FAST", 0.94)
    wrapper, loops = _wrapper(records, checkpoint, router)

    await wrapper.run(_context(), media_id=7, profile=MODE_PROFILE)

    record = repository.get("execution-j1c")
    assert record is not None
    assert record.execution_contract_version == EXECUTION_CONTRACT_VERSION_V2
    assert [event.event_type for event in record.events[:2]] == [
        ExecutionEventType.EXECUTION_STARTED,
        ExecutionEventType.MODEL_ROUTE_RECORDED,
    ]
    assert record.events[1].payload["lane"] == "FAST"
    assert record.events[1].payload["resolvedModelId"] == "fast-model-a"
    assert loops[ModelRouteLane.FAST].calls == 1

    recovered_router = _Router("DEEP", 0.99)
    recovered, recovered_loops = _wrapper(
        records,
        checkpoint,
        recovered_router,
    )
    await recovered.run(_context(), media_id=7, profile=MODE_PROFILE)
    assert recovered_router.calls == 0
    assert recovered_loops[ModelRouteLane.FAST].calls == 1
    assert recovered_loops[ModelRouteLane.DEEP].calls == 0
    assert len(
        [event for event in repository.get("execution-j1c").events
         if event.event_type is ExecutionEventType.MODEL_ROUTE_RECORDED]
    ) == 1


@pytest.mark.asyncio
async def test_route_event_is_durable_before_real_agent_loop_planner_call() -> None:
    records, repository = _records()
    checkpoint = _Checkpoint()
    calls = []
    context = _context()

    class _ContextService:
        async def select_relevant(self, value, media_id=None):
            del media_id
            calls.append("retrieval")
            return value

    class _Planner:
        async def plan(self, value, *, instruction=""):
            del instruction
            calls.append("planner")
            return AgentPlan(understoodGoal=value.user_goal, tasks=("inspect",))

        async def repair_plan(self, value, invalid, *, instruction=""):
            del value, invalid, instruction
            raise AssertionError("plan repair was not expected")

        async def replan(self, value, current, critique, *, instruction=""):
            del value, critique, instruction
            return current

    class _Executor:
        async def execute(self, value, plan, previous_critique=None, *, instruction=""):
            del value, plan, previous_critique, instruction
            calls.append("executor")
            return AnalysisResult(
                title="answer",
                conclusions=("opening evidence",),
                evidence=(
                    AnalysisEvidence(
                        timestampMs=0,
                        source="ASR",
                        content="opening evidence",
                        claim="opening evidence",
                        sourceRevision=REVISION,
                        segmentId="segment-1",
                        sourceItemIds=("item-1",),
                        sourceProvenanceVersion=PROVENANCE_VERSION,
                    ),
                ),
            )

    class _Critic:
        async def critique(self, value, plan, result, *, instruction=""):
            del value, plan, result, instruction
            calls.append("critic")
            return CriticResult(passed=True)

    lane = AgentLoopService(
        context_service=_ContextService(),
        planner=_Planner(),
        executor=_Executor(),
        critic=_Critic(),
        execution_record_service=records,
        budget_config={"maxRounds": 1},
    )
    router = _Router("FAST", 0.94)
    wrapper = ProductionModelRoutingAgentLoop(
        {lane_name: lane for lane_name in ModelRouteLane},
        ModelRoutingService(router, policy=ModelRoutingPolicy(adaptive_routing_enabled=True)),
        checkpoint=checkpoint,
        execution_records=records,
        resolved_model_ids={
            ModelRouteLane.BALANCED: "balanced-model-a",
            ModelRouteLane.FAST: "fast-model-a",
            ModelRouteLane.DEEP: "deep-model-a",
        },
        require_historical_route=True,
    )

    await wrapper.run(context, media_id=7, profile=MODE_PROFILE)
    record = repository.get("execution-j1c")
    assert calls == ["retrieval", "planner", "executor", "critic"]
    route_index = next(
        index
        for index, event in enumerate(record.events)
        if event.event_type is ExecutionEventType.MODEL_ROUTE_RECORDED
    )
    plan_index = next(
        index
        for index, event in enumerate(record.events)
        if event.event_type is ExecutionEventType.PLAN_RECORDED
    )
    assert route_index < plan_index


@pytest.mark.asyncio
async def test_route_event_recovers_when_checkpoint_is_missing_without_router_call() -> None:
    records, repository = _records()
    checkpoint = _Checkpoint()
    first_router = _Router("DEEP", 0.91)
    first, _ = _wrapper(records, checkpoint, first_router)
    await first.run(_context(), media_id=7, profile=MODE_PROFILE)
    checkpoint.values.clear()

    second_router = _Router("FAST", 0.99)
    second, loops = _wrapper(records, checkpoint, second_router)
    await second.run(_context(), media_id=7, profile=MODE_PROFILE)
    assert second_router.calls == 0
    assert loops[ModelRouteLane.DEEP].calls == 1
    assert checkpoint.saves == 2


@pytest.mark.asyncio
async def test_checkpoint_route_conflict_fails_closed_before_provider_lane() -> None:
    records, repository = _records()
    checkpoint = _Checkpoint()
    first_router = _Router("FAST", 0.94)
    first, _ = _wrapper(records, checkpoint, first_router)
    await first.run(_context(), media_id=7, profile=MODE_PROFILE)
    checkpoint.values[TaskKey(7, _context().user_goal, AnalysisMode.GENERAL)] = (
        ModelRoutingDecision(
            lane=ModelRouteLane.DEEP,
            confidence=0.94,
            fallbackUsed=False,
            reasonCode=ModelRoutingReasonCode.ROUTER_ACCEPTED,
            routingContractVersion="model-routing-v1",
            suggestedLane=ModelRouteLane.DEEP,
        )
    )

    second, loops = _wrapper(records, checkpoint, _Router("DEEP"))
    with pytest.raises(ModelRoutingHistoryIntegrityError):
        await second.run(_context(), media_id=7, profile=MODE_PROFILE)
    assert loops[ModelRouteLane.FAST].calls == 0
    assert loops[ModelRouteLane.DEEP].calls == 0
    assert repository.get("execution-j1c").events[1].payload["lane"] == "FAST"


@pytest.mark.asyncio
async def test_disabled_routing_records_balanced_reason_without_jev() -> None:
    records, repository = _records()
    checkpoint = _Checkpoint()
    router = _Router("FAST")
    wrapper, loops = _wrapper(
        records,
        checkpoint,
        router,
        enabled=False,
        lanes={ModelRouteLane.BALANCED},
        resolved={ModelRouteLane.BALANCED: "balanced-model-a"},
    )
    await wrapper.run(_context(), media_id=7, profile=MODE_PROFILE)
    event = repository.get("execution-j1c").events[1]
    assert router.calls == 0
    assert loops[ModelRouteLane.BALANCED].calls == 1
    assert event.event_type is ExecutionEventType.MODEL_ROUTE_RECORDED
    assert event.payload["lane"] == "BALANCED"
    assert event.payload["reasonCode"] == "ROUTING_DISABLED"
    assert checkpoint.saves == 0


@pytest.mark.asyncio
async def test_removed_historical_profile_fails_closed_without_resampling() -> None:
    records, checkpoint_store = _records()
    first, _ = _wrapper(records, checkpoint_store, _Router("FAST"))
    await first.run(_context(), media_id=7, profile=MODE_PROFILE)
    second, loops = _wrapper(
        records,
        checkpoint_store,
        _Router("DEEP"),
        lanes={ModelRouteLane.BALANCED},
        resolved={ModelRouteLane.BALANCED: "balanced-model-a"},
    )
    with pytest.raises(RoutingProfileUnavailableError):
        await second.run(_context(), media_id=7, profile=MODE_PROFILE)
    assert loops[ModelRouteLane.BALANCED].calls == 0


@pytest.mark.asyncio
async def test_route_event_idempotency_and_conflicting_duplicate() -> None:
    records, repository = _records()
    key = TaskKey(7, "explain the evidence", AnalysisMode.GENERAL)
    record = await records.start_or_resume(
        key,
        media_identity="sha256:video-j1c",
        source_revision=REVISION,
    )
    decision = ModelRoutingDecision(
        lane=ModelRouteLane.FAST,
        confidence=0.94,
        fallbackUsed=False,
        reasonCode=ModelRoutingReasonCode.ROUTER_ACCEPTED,
        routingContractVersion="model-routing-v1",
        suggestedLane=ModelRouteLane.FAST,
    )
    profile = ModelProfileRegistry.default().resolve(ModelRouteLane.FAST)
    first = await records.record_model_route(
        record.execution_id,
        decision,
        profile=profile,
        resolved_model_id="fast-model-a",
    )
    duplicate = await records.record_model_route(
        record.execution_id,
        decision,
        profile=profile,
        resolved_model_id="fast-model-a",
    )
    assert first.sequence_no == duplicate.sequence_no == 2
    with pytest.raises(ExecutionRecordConflictError):
        await records.record_model_route(
            record.execution_id,
            decision.model_copy(update={"confidence": 0.95}),
            profile=profile,
            resolved_model_id="fast-model-a",
        )
    assert len(repository.get(record.execution_id).events) == 2


async def _completed_v2_with_route():
    records, repository = _records(contract=EXECUTION_CONTRACT_VERSION_V2)
    key = TaskKey(7, "explain the evidence", AnalysisMode.GENERAL)
    record = await records.start_or_resume(
        key,
        media_identity="sha256:video-j1c",
        source_revision=REVISION,
    )
    decision = ModelRoutingDecision(
        lane=ModelRouteLane.FAST,
        confidence=0.72,
        fallbackUsed=False,
        reasonCode=ModelRoutingReasonCode.ROUTER_ACCEPTED,
        routingContractVersion="model-routing-v1",
        suggestedLane=ModelRouteLane.FAST,
    )
    await records.record_model_route(
        record.execution_id,
        decision,
        profile=ModelProfileRegistry.default().resolve(ModelRouteLane.FAST),
        resolved_model_id="model-A",
    )
    await records.record_retrieval_selection(
        record.execution_id,
        [],
        purpose="initial",
        logical_event_id="retrieval.initial",
    )
    plan = AgentPlan(understoodGoal=key.goal, tasks=("inspect",))
    await records.record_plan(record.execution_id, plan)
    result = AnalysisResult(title="answer", conclusions=("claim",))
    await records.record_executor_turn(
        record.execution_id,
        ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=result),
        logical_event_id="executor.final",
        agent_round=0,
    )
    critic = CriticResult(passed=True)
    await records.record_critic(
        record.execution_id,
        critic,
        logical_event_id="critic.final",
        agent_round=0,
    )
    await records.record_evidence_verification(
        record.execution_id,
        result,
        critic,
        logical_event_id="evidence.final",
        agent_round=0,
        source_revision=REVISION,
    )
    completed = await records.complete(
        record.execution_id,
        AgentState(
            goal=key.goal,
            plan=plan,
            result=result,
            critique=critic,
            round=0,
        ),
    )
    return records, repository, completed


@pytest.mark.asyncio
async def test_v2_replay_returns_recorded_route_without_policy_or_jev() -> None:
    _records_service, repository, completed = await _completed_v2_with_route()
    replay = await HistoricalAgentReplayService(repository).replay(
        completed.execution_id
    )
    assert replay.historical_model_route is not None
    assert replay.historical_model_route.lane is ModelRouteLane.FAST
    assert replay.historical_model_route.confidence == 0.72
    assert replay.historical_model_route.resolved_model_id == "model-A"
    assert replay.execution_contract_version == EXECUTION_CONTRACT_VERSION_V2


@pytest.mark.asyncio
async def test_v2_missing_route_is_incomplete_and_v1_remains_compatible() -> None:
    records, repository = _records(contract=EXECUTION_CONTRACT_VERSION_V2)
    key = TaskKey(7, "v2 missing route", AnalysisMode.GENERAL)
    record = await records.start_or_resume(
        key,
        media_identity="media",
        source_revision=REVISION,
    )
    await records.record_plan(
        record.execution_id,
        AgentPlan(understoodGoal=key.goal, tasks=("inspect",)),
    )
    failed = await records.fail(record.execution_id, RuntimeError("bounded"))
    with pytest.raises(ReplayIncompleteError):
        await HistoricalAgentReplayService(repository).replay(failed.execution_id)

    v1_records, v1_repository = _records(contract=EXECUTION_CONTRACT_VERSION_V1)
    v1_key = TaskKey(7, "legacy route", AnalysisMode.GENERAL)
    v1_record = await v1_records.start_or_resume(
        v1_key,
        media_identity="media",
        source_revision=REVISION,
    )
    v1_failed = await v1_records.fail(v1_record.execution_id, RuntimeError("bounded"))
    result = await HistoricalAgentReplayService(v1_repository).replay(
        v1_failed.execution_id
    )
    assert result.historical_model_route is None


@pytest.mark.asyncio
async def test_v2_route_after_plan_and_multiple_route_events_fail_closed() -> None:
    records, repository = _records(contract=EXECUTION_CONTRACT_VERSION_V2)
    key = TaskKey(7, "invalid route order", AnalysisMode.GENERAL)
    record = await records.start_or_resume(
        key,
        media_identity="media",
        source_revision=REVISION,
    )
    plan = AgentPlan(understoodGoal=key.goal, tasks=("inspect",))
    await records.record_plan(record.execution_id, plan)
    decision = ModelRoutingDecision(
        lane=ModelRouteLane.BALANCED,
        confidence=0.0,
        fallbackUsed=True,
        reasonCode=ModelRoutingReasonCode.ROUTING_DISABLED,
        routingContractVersion="model-routing-v1",
    )
    await records.record_model_route(
        record.execution_id,
        decision,
        profile=ModelProfileRegistry.default().resolve(ModelRouteLane.BALANCED),
        resolved_model_id="balanced-model-a",
    )
    failed = await records.fail(record.execution_id, RuntimeError("bounded"))
    with pytest.raises(ReplayIntegrityError):
        await HistoricalAgentReplayService(repository).replay(failed.execution_id)

    records2, repository2, completed = await _completed_v2_with_route()
    route = next(
        event
        for event in repository2.get(completed.execution_id).events
        if event.event_type is ExecutionEventType.MODEL_ROUTE_RECORDED
    )
    # A terminal record cannot be appended to through the repository service;
    # inject the second actual event before the terminal event for corruption
    # testing, preserving contiguous sequence numbers.
    original = repository2.get(completed.execution_id)
    route_index = next(
        index
        for index, event in enumerate(original.events)
        if event.event_type is ExecutionEventType.MODEL_ROUTE_RECORDED
    )
    duplicate = route.model_copy(
        update={
            "sequence_no": route.sequence_no + 1,
            "logical_event_id": "model.route.duplicate",
        }
    )
    shifted = [*original.events[: route_index + 1], duplicate]
    shifted.extend(
        event.model_copy(update={"sequence_no": event.sequence_no + 1})
        for event in original.events[route_index + 1 :]
    )
    repository2._records[completed.execution_id] = original.model_copy(
        update={"events": tuple(shifted)}
    )
    with pytest.raises(ReplayIntegrityError):
        await HistoricalAgentReplayService(repository2).replay(completed.execution_id)


def test_r4_disabled_composition_wires_route_history_without_network(monkeypatch) -> None:
    import dovideo.infrastructure.r4_runtime as runtime

    monkeypatch.setattr(
        runtime.ProviderConfig,
        "from_environment",
        classmethod(
            lambda cls, environ=None, *, prefix="DOVIDEO_", required=False: ProviderConfig(
                base_url="https://provider.invalid/v1",
                model="balanced-model-a",
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

    class _Trace:
        def start(self, key):
            del key
            return "trace"

        def latest(self, key):
            del key
            return {}

        def record_structural_for_key(self, key, diagnostic):
            del key, diagnostic

        def increment_for_key(self, key, metric, amount=1):
            del key, metric, amount

        def observe_for_key(self, key, metric, value):
            del key, metric, value

        def add_usage_for_key(self, key, estimated_tokens=0, estimated_cost=0.0, usage=None):
            del key, estimated_tokens, estimated_cost, usage
            return {"estimatedTokens": 0, "estimatedCost": 0.0}

        def current_usage_for_key(self, key):
            del key
            return {"estimatedTokens": 0, "estimatedCost": 0.0}

    records, _repository = _records()
    checkpoint = _Checkpoint()
    stack = create_r4_provider_stack(
        checkpoint,
        object(),
        R4AgentTelemetry(_Trace()),
        None,
        tool_settings=X1ToolCallingSettings(enabled=False),
        execution_records=records,
        routing_settings=ModelRoutingProductionSettings(
            enabled=False,
            balanced_model="balanced-model-a",
        ),
    )
    try:
        assert isinstance(stack.agent_loop, ProductionModelRoutingAgentLoop)
        assert stack.agent_loop.durable_history is True
        assert stack.routing_enabled is False
        assert stack.resolved_model_ids[ModelRouteLane.BALANCED] == "balanced-model-a"
    finally:
        import asyncio

        asyncio.run(stack.close())

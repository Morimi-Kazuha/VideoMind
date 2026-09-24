from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application import (
    AgentLoopService,
    DurableToolCallState,
    DurableToolExecutionState,
    DurableToolStateLedger,
    ExecutorTurn,
    ExecutorTurnKind,
    ModelToolRequest,
    PolicyDecision,
    ToolCallingDisabledWithInFlightStateError,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    VideoReadOnlyToolExecutor,
    canonical_tool_arguments_digest,
    mode_profile_for,
)
from dovideo.application.tool_contracts import GetSegmentArguments
from dovideo.application.value_objects import TaskKey
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.providers import ProviderConfig
from dovideo.infrastructure.r4_runtime import R4AgentTelemetry, create_r4_provider_stack
from dovideo.infrastructure.x1_config import X1ConfigurationError, X1ToolCallingSettings


def _context() -> VideoContext:
    return VideoContext(
        source="memory://video",
        user_goal="explain",
        segments=(VideoSegment(start_ms=0, end_ms=60_000, transcript="opening"),),
    )


def _plan() -> AgentPlan:
    return AgentPlan(understood_goal="explain", tasks=("ground the claim",))


def _result(*, bad_evidence: bool = False) -> AnalysisResult:
    return AnalysisResult(
        title="grounded result",
        conclusions=("opening",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=1_000,
                source="ASR",
                content="not in the video" if bad_evidence else "opening",
                claim="not the conclusion" if bad_evidence else "opening",
            ),
        ),
    )


def _final(*, bad_evidence: bool = False) -> ExecutorTurn:
    return ExecutorTurn(
        kind=ExecutorTurnKind.FINAL,
        final_result=_result(bad_evidence=bad_evidence),
    )


def _request(name: str = "video.get_segment") -> ExecutorTurn:
    return ExecutorTurn(
        kind=ExecutorTurnKind.TOOL_REQUEST,
        tool_request=ModelToolRequest(
            tool_name=name,
            arguments={"timestamp_ms": 1_000} if name == "video.get_segment" else {},
        ),
    )


def _success(call_id: str = "tool-call-1") -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name="video.get_segment",
        status=ToolResultStatus.SUCCESS,
        payload={"text": "opening"},
    )


class _Context:
    async def select_relevant(self, context: VideoContext, media_id: int | None = None) -> VideoContext:
        del media_id
        return context

    async def refine_for_critique(self, media_id, full_context, selected_context, critique=None):
        del media_id, full_context, critique
        return selected_context


class _Planner:
    async def plan(self, context, *, instruction=""):
        del context, instruction
        return _plan()

    async def repair_plan(self, context, invalid_plan, *, instruction=""):
        del context, invalid_plan, instruction
        return _plan()

    async def replan(self, context, current_plan, critique, *, instruction=""):
        del context, current_plan, critique, instruction
        return _plan()


class _LegacyExecutor:
    def __init__(self, result: AnalysisResult | None = None) -> None:
        self.calls = 0
        self.result = result or _result()

    async def execute(self, context, plan, previous_critique=None, *, instruction=""):
        del context, plan, previous_critique, instruction
        self.calls += 1
        return self.result


class _Turns:
    def __init__(self, turns: list[ExecutorTurn]) -> None:
        self.turns = list(turns)
        self.initial_calls = 0
        self.continuations: list[tuple[ToolResult, bool]] = []

    async def execute_turn(self, context, plan, previous_critique=None, *, instruction=""):
        del context, plan, previous_critique, instruction
        self.initial_calls += 1
        return self.turns.pop(0)

    async def continue_after_tool(
        self,
        context,
        plan,
        tool_result,
        previous_critique=None,
        *,
        instruction="",
        tools_available=True,
    ):
        del context, plan, previous_critique, instruction
        self.continuations.append((tool_result, tools_available))
        return self.turns.pop(0)


class _Critic:
    async def critique(self, context, plan, result, *, instruction=""):
        del context, plan, result, instruction
        return CriticResult(passed=True)


class _Telemetry:
    def __init__(self, *, fail_tool_metrics: bool = False) -> None:
        self.counts: Counter[str] = Counter()
        self.fail_tool_metrics = fail_tool_metrics

    def increment(self, metric: str, amount: int = 1, **_kwargs) -> None:
        if self.fail_tool_metrics and metric.startswith("tool"):
            raise RuntimeError("trace backend unavailable")
        self.counts[metric] += amount

    def observe(self, metric: str, value: float, **_kwargs) -> None:
        del metric, value


class _Checkpoint:
    def __init__(self, ledger: DurableToolStateLedger | None = None) -> None:
        self.ledger = ledger
        self.tool_saves = 0
        self.fail_tool_save = False
        self.state: AgentState | None = None

    async def load_plan(self, key):
        del key
        return None

    async def save_plan(self, key, plan):
        del key, plan

    async def load_critic_state(self, key):
        del key
        return self.state

    async def save_execution_state(self, key, state):
        del key
        self.state = state

    async def save_critic_state(self, key, state):
        del key
        self.state = state

    async def save_result(self, key, state):
        del key
        self.state = state

    async def load_tool_state(self, key):
        assert self.ledger is None or self.ledger.task_key == key
        return self.ledger

    async def save_tool_state(self, key, state):
        assert state.task_key == key
        self.tool_saves += 1
        if self.fail_tool_save:
            raise RuntimeError("checkpoint unavailable")
        self.ledger = state


class _CountingToolExecutor:
    def __init__(self, result: ToolResult | None = None) -> None:
        self.calls = 0
        self.result = result or _success()

    async def execute(self, tool_call, trusted_context, remaining_deadline=None):
        del trusted_context, remaining_deadline
        self.calls += 1
        return self.result.model_copy(update={"call_id": tool_call.call_id})


def _stored_ledger(
    state: DurableToolExecutionState = DurableToolExecutionState.RESULT_STORED,
) -> DurableToolStateLedger:
    key = TaskKey(7, "explain", AnalysisMode.GENERAL)
    request = ModelToolRequest(
        tool_name="video.get_segment",
        arguments={"timestamp_ms": 1_000},
    )
    requested = DurableToolCallState(
        task_key=key,
        agent_round=1,
        request_index=1,
        call_id="tool-call-1",
        tool_name=request.tool_name,
        request=request,
        execution_state=DurableToolExecutionState.REQUESTED,
    )
    authorized = requested.transition(
        DurableToolExecutionState.AUTHORIZED,
        validated_arguments=GetSegmentArguments(timestamp_ms=1_000),
        canonical_args_digest=canonical_tool_arguments_digest(
            GetSegmentArguments(timestamp_ms=1_000)
        ),
        policy_decision=PolicyDecision.ALLOW,
    )
    if state is DurableToolExecutionState.AUTHORIZED:
        return DurableToolStateLedger(task_key=key, records=(authorized,))
    executing = authorized.transition(DurableToolExecutionState.EXECUTING)
    if state is DurableToolExecutionState.EXECUTING:
        return DurableToolStateLedger(task_key=key, records=(executing,))
    stored = executing.transition(
        DurableToolExecutionState.RESULT_STORED,
        tool_result=_success(),
    )
    return DurableToolStateLedger(task_key=key, records=(stored,))


def _service(
    *,
    turns: list[ExecutorTurn],
    enabled: bool,
    tool_checkpoint: _Checkpoint,
    telemetry: _Telemetry | None = None,
    tool_executor=None,
    legacy_executor: _LegacyExecutor | None = None,
) -> tuple[AgentLoopService, _Turns, _LegacyExecutor]:
    turn_port = _Turns(turns)
    legacy = legacy_executor or _LegacyExecutor()
    service = AgentLoopService(
        _Context(),
        _Planner(),
        legacy,
        tool_checkpoint,
        None,
        telemetry or _Telemetry(),
        _Critic(),
        executor_turn=turn_port if enabled else None,
        tool_executor=tool_executor if enabled else None,
        tool_checkpoint=tool_checkpoint,
        tool_calling_enabled=enabled,
        tool_policy=ToolPolicy(ToolRegistry()),
        budget_config={"max_rounds": 1, "max_duration_ms": 10_000},
    )
    return service, turn_port, legacy


def _run_kwargs() -> dict[str, object]:
    return {
        "context": _context(),
        "media_id": 7,
        "profile": mode_profile_for(AnalysisMode.GENERAL),
    }


def test_x1_config_defaults_disabled_and_rejects_unsafe_values() -> None:
    defaults = X1ToolCallingSettings.from_environment({})
    assert defaults.enabled is False
    assert defaults.per_round_limit == 4
    assert defaults.total_limit == 12
    assert defaults.tool_result_limit_bytes == 64 * 1024

    with pytest.raises(X1ConfigurationError):
        X1ToolCallingSettings.from_environment(
            {"DOVIDEO_AGENT_TOOL_CALLING_ENABLED": "maybe"}
        )
    with pytest.raises(X1ConfigurationError):
        X1ToolCallingSettings.from_environment(
            {"DOVIDEO_AGENT_TOOL_REQUEST_LIMIT_TOTAL": "0"}
        )
    with pytest.raises(X1ConfigurationError):
        X1ToolCallingSettings(tool_result_limit_bytes=64 * 1024 + 1)


@pytest.mark.asyncio
async def test_disabled_mode_uses_legacy_executor_without_tool_ledger_write() -> None:
    checkpoint = _Checkpoint()
    legacy = _LegacyExecutor()
    service, turns, _ = _service(
        turns=[],
        enabled=False,
        tool_checkpoint=checkpoint,
        legacy_executor=legacy,
    )
    state = await service.run_once(**_run_kwargs())
    assert state.result == _result()
    assert legacy.calls == 1
    assert turns.initial_calls == 0
    assert checkpoint.tool_saves == 0
    assert checkpoint.ledger is None


@pytest.mark.asyncio
async def test_enabled_final_path_reaches_critic_without_tool_execution() -> None:
    checkpoint = _Checkpoint()
    service, turns, legacy = _service(
        turns=[_final()],
        enabled=True,
        tool_checkpoint=checkpoint,
        tool_executor=_CountingToolExecutor(),
    )
    state = await service.run(**_run_kwargs())
    assert state.critique is not None and state.critique.passed
    assert turns.initial_calls == 1
    assert legacy.calls == 0
    assert checkpoint.tool_saves == 0


@pytest.mark.asyncio
async def test_enabled_successful_tool_is_durable_then_continues_and_counts() -> None:
    checkpoint = _Checkpoint()
    telemetry = _Telemetry()
    concrete = VideoReadOnlyToolExecutor()
    service, turns, _ = _service(
        turns=[_request(), _final()],
        enabled=True,
        tool_checkpoint=checkpoint,
        telemetry=telemetry,
        tool_executor=concrete,
    )
    state = await service.run(**_run_kwargs())
    assert state.critique is not None and state.critique.passed
    assert turns.initial_calls == 1
    assert len(turns.continuations) == 1
    assert checkpoint.ledger is not None
    assert checkpoint.ledger.records[-1].execution_state is DurableToolExecutionState.CONSUMED
    assert telemetry.counts["toolRequests"] == 1
    assert telemetry.counts["toolAllowed"] == 1
    assert telemetry.counts["toolExecuted"] == 1
    assert telemetry.counts["toolDenied"] == 0
    assert telemetry.counts["toolFailures"] == 0
    assert telemetry.counts["toolResultTruncated"] == 0


@pytest.mark.asyncio
async def test_policy_denial_is_typed_continuation_and_never_executes_tool() -> None:
    checkpoint = _Checkpoint()
    telemetry = _Telemetry()
    concrete = _CountingToolExecutor()
    service, turns, _ = _service(
        turns=[_request("shell.exec"), _final()],
        enabled=True,
        tool_checkpoint=checkpoint,
        telemetry=telemetry,
        tool_executor=concrete,
    )
    state = await service.run_once(**_run_kwargs())
    assert state.result == _result()
    assert concrete.calls == 0
    assert turns.continuations[0][0].status is ToolResultStatus.DENIED
    assert telemetry.counts["toolRequests"] == 1
    assert telemetry.counts["toolDenied"] == 1
    assert telemetry.counts["toolExecuted"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "metric"),
    [
        (
            ToolResult(
                call_id="tool-call-1",
                tool_name="video.get_segment",
                status=ToolResultStatus.FAILED,
            ),
            "toolFailures",
        ),
        (
            ToolResult(
                call_id="tool-call-1",
                tool_name="video.get_segment",
                status=ToolResultStatus.TRUNCATED,
                truncated=True,
            ),
            "toolResultTruncated",
        ),
    ],
)
async def test_typed_tool_failure_and_truncation_metrics(
    result: ToolResult,
    metric: str,
) -> None:
    checkpoint = _Checkpoint()
    telemetry = _Telemetry()
    concrete = _CountingToolExecutor(result)
    service, _turns, _ = _service(
        turns=[_request(), _final()],
        enabled=True,
        tool_checkpoint=checkpoint,
        telemetry=telemetry,
        tool_executor=concrete,
    )
    await service.run_once(**_run_kwargs())
    assert telemetry.counts[metric] == 1
    assert telemetry.counts["toolExecuted"] == 1


@pytest.mark.asyncio
async def test_checkpoint_failure_after_request_denies_tool_execution() -> None:
    checkpoint = _Checkpoint()
    checkpoint.fail_tool_save = True
    concrete = _CountingToolExecutor()
    service, _turns, _ = _service(
        turns=[_request()],
        enabled=True,
        tool_checkpoint=checkpoint,
        tool_executor=concrete,
    )
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await service.run_once(**_run_kwargs())
    assert concrete.calls == 0


@pytest.mark.asyncio
async def test_result_stored_recovery_reuses_result_without_concrete_execution() -> None:
    checkpoint = _Checkpoint(_stored_ledger())
    telemetry = _Telemetry()
    concrete = _CountingToolExecutor()
    service, turns, _ = _service(
        turns=[_final()],
        enabled=True,
        tool_checkpoint=checkpoint,
        telemetry=telemetry,
        tool_executor=concrete,
    )
    await service.run_once(**_run_kwargs())
    assert turns.initial_calls == 0
    assert concrete.calls == 0
    assert telemetry.counts["toolRecovered"] == 1
    assert telemetry.counts["toolResultReused"] == 1


@pytest.mark.asyncio
async def test_executing_recovery_is_at_least_once_with_same_call_identity() -> None:
    checkpoint = _Checkpoint(_stored_ledger(DurableToolExecutionState.EXECUTING))
    concrete = _CountingToolExecutor()
    service, turns, _ = _service(
        turns=[_final()],
        enabled=True,
        tool_checkpoint=checkpoint,
        tool_executor=concrete,
    )
    await service.run_once(**_run_kwargs())
    assert turns.initial_calls == 0
    assert concrete.calls == 1
    assert checkpoint.ledger is not None
    assert checkpoint.ledger.records[-1].call_id == "tool-call-1"


@pytest.mark.asyncio
async def test_disabling_feature_with_inflight_state_fails_closed() -> None:
    checkpoint = _Checkpoint(_stored_ledger(DurableToolExecutionState.AUTHORIZED))
    legacy = _LegacyExecutor()
    service, _turns, _ = _service(
        turns=[],
        enabled=False,
        tool_checkpoint=checkpoint,
        legacy_executor=legacy,
    )
    with pytest.raises(ToolCallingDisabledWithInFlightStateError):
        await service.run_once(**_run_kwargs())
    assert legacy.calls == 0


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_change_tool_correctness() -> None:
    checkpoint = _Checkpoint()
    telemetry = _Telemetry(fail_tool_metrics=True)
    concrete = VideoReadOnlyToolExecutor()
    service, _turns, _ = _service(
        turns=[_request(), _final()],
        enabled=True,
        tool_checkpoint=checkpoint,
        telemetry=telemetry,
        tool_executor=concrete,
    )
    state = await service.run(**_run_kwargs())
    assert state.critique is not None and state.critique.passed
    assert checkpoint.ledger is not None
    assert checkpoint.ledger.records[-1].execution_state is DurableToolExecutionState.CONSUMED


@pytest.mark.asyncio
async def test_tool_result_remains_untrusted_to_evidence_guard() -> None:
    checkpoint = _Checkpoint()
    service, _turns, _ = _service(
        turns=[_request(), _final(bad_evidence=True)],
        enabled=True,
        tool_checkpoint=checkpoint,
        tool_executor=VideoReadOnlyToolExecutor(),
    )
    state = await service.run(**_run_kwargs())
    assert state.critique is not None
    assert state.critique.passed is False


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

    def current_usage_for_key(self, key):
        del key
        return {"estimatedTokens": 0, "estimatedCost": 0.0}


@pytest.mark.asyncio
async def test_production_stack_wires_x1_without_network_or_provider_call(monkeypatch) -> None:
    import dovideo.infrastructure.r4_runtime as runtime

    def _provider_config(cls, environ=None, *, prefix="DOVIDEO_", required=False):
        del cls, environ, prefix, required
        return ProviderConfig(base_url="http://provider.invalid/v1", model="test-model")

    monkeypatch.setattr(
        runtime.ProviderConfig,
        "from_environment",
        classmethod(_provider_config),
    )
    monkeypatch.setattr(
        runtime,
        "embedding_provider_config_from_environment",
        lambda environ=None, *, required=True: ProviderConfig(
            base_url="http://embedding.invalid/v1",
            model="BAAI/bge-m3",
            embedding_model="BAAI/bge-m3",
        ),
    )
    checkpoint = _Checkpoint()
    telemetry = R4AgentTelemetry(_TraceStore())
    settings = X1ToolCallingSettings(enabled=True)
    stack = create_r4_provider_stack(
        checkpoint,
        object(),
        telemetry,
        None,
        tool_settings=settings,
    )
    try:
        assert stack.tool_settings == settings
        assert stack.tool_registry.names() == (
            "video.search_evidence",
            "video.get_segment",
            "video.get_context_window",
        )
        assert isinstance(stack.tool_executor, VideoReadOnlyToolExecutor)
        agent = stack.agent_loop
        assert getattr(agent, "_executor_turn") is not None
        assert getattr(agent, "_tool_checkpoint") is checkpoint
        assert getattr(agent, "_tool_policy").registry is stack.tool_registry
    finally:
        await stack.close()

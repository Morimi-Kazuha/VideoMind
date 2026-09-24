from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application import (
    AgentBudgetConfig,
    AgentExecutionBudget,
    AgentLoopService,
    ExecutorTurn,
    ExecutorTurnKind,
    ModelToolRequest,
    ToolPolicy,
    ToolPolicyReasonCode,
    ToolResult,
    ToolResultStatus,
    ToolTurnBudgetExceededError,
)
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisResult,
    CriticResult,
    VideoContext,
    VideoSegment,
)


def _context() -> VideoContext:
    return VideoContext(
        source="memory://video",
        user_goal="explain the evidence",
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=60_000,
                transcript="opening statement",
            ),
        ),
    )


def _plan() -> AgentPlan:
    return AgentPlan(understood_goal="explain", tasks=("ground the claim",))


def _result() -> AnalysisResult:
    return AnalysisResult(
        title="grounded result",
        conclusions=("opening statement",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=1_000,
                source="ASR",
                content="opening statement",
                claim="opening statement",
            ),
        ),
    )


def _request(
    tool_name: str = "video.get_segment",
    arguments: dict[str, object] | None = None,
) -> ExecutorTurn:
    return ExecutorTurn(
        kind=ExecutorTurnKind.TOOL_REQUEST,
        tool_request=ModelToolRequest(
            tool_name=tool_name,
            arguments=arguments or {"timestamp_ms": 1_000},
        ),
    )


def _final() -> ExecutorTurn:
    return ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=_result())


class ContextFake:
    async def select_relevant(
        self,
        context: VideoContext,
        media_id: int | None = None,
    ) -> VideoContext:
        del media_id
        return context


class PlannerFake:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        del context, instruction
        self.calls += 1
        return _plan()

    async def repair_plan(
        self,
        context: VideoContext,
        invalid_plan: AgentPlan,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        del context, invalid_plan, instruction
        return _plan()

    async def replan(
        self,
        context: VideoContext,
        current_plan: AgentPlan,
        critique: CriticResult,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        del context, current_plan, critique, instruction
        return _plan()


class LegacyExecutorFake:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> AnalysisResult:
        del context, plan, previous_critique, instruction
        self.calls += 1
        return _result()


class CriticFake:
    def __init__(self) -> None:
        self.calls: list[AnalysisResult | None] = []

    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult | None,
        *,
        instruction: str = "",
    ) -> CriticResult:
        del context, plan, instruction
        self.calls.append(result)
        return CriticResult(passed=True)


class CheckpointFake:
    async def load_plan(self, key: object) -> AgentPlan | None:
        del key
        return None

    async def save_plan(self, key: object, plan: AgentPlan) -> None:
        del key, plan

    async def load_critic_state(self, key: object) -> AgentState | None:
        del key
        return None

    async def save_execution_state(self, key: object, state: AgentState) -> None:
        del key, state

    async def save_critic_state(self, key: object, state: AgentState) -> None:
        del key, state

    async def save_result(self, key: object, state: AgentState) -> None:
        del key, state


class PublisherFake:
    async def publish(self, key: object, event: object) -> None:
        del key, event


class TelemetryFake:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount


class TurnFake:
    def __init__(self, turns: list[ExecutorTurn]) -> None:
        self.turns = list(turns)
        self.initial_calls = 0
        self.continuation_calls: list[tuple[ToolResult, bool]] = []

    async def execute_turn(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> ExecutorTurn:
        del context, plan, previous_critique, instruction
        self.initial_calls += 1
        return self.turns.pop(0)

    async def continue_after_tool(
        self,
        context: VideoContext,
        plan: AgentPlan,
        tool_result: ToolResult,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
        tools_available: bool = True,
    ) -> ExecutorTurn:
        del context, plan, previous_critique, instruction
        self.continuation_calls.append((tool_result, tools_available))
        return self.turns.pop(0)


class ToolFake:
    def __init__(self, results: list[ToolResult], clock: object | None = None) -> None:
        self.results = list(results)
        self.calls: list[tuple[object, object, float | None]] = []
        self._clock = clock

    async def execute(
        self,
        tool_call: object,
        trusted_context: object,
        remaining_deadline: float | None = None,
    ) -> ToolResult:
        self.calls.append((tool_call, trusted_context, remaining_deadline))
        advance = getattr(self._clock, "advance", None)
        if callable(advance):
            advance(0.005)
        return self.results.pop(0)


def _success(call_id: str = "tool-call-1") -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name="video.get_segment",
        status=ToolResultStatus.SUCCESS,
        payload={"timestamp_ms": 1_000, "text": "opening statement"},
    )


def _failed() -> ToolResult:
    return ToolResult(
        call_id="tool-call-1",
        tool_name="video.get_segment",
        status=ToolResultStatus.FAILED,
    )


def _service(
    turn: TurnFake,
    tool: ToolFake,
    critic: CriticFake,
    *,
    per_round: int = 4,
    total: int = 12,
    execution_budget: AgentExecutionBudget | None = None,
) -> AgentLoopService:
    return AgentLoopService(
        ContextFake(),
        PlannerFake(),
        LegacyExecutorFake(),
        CheckpointFake(),
        PublisherFake(),
        TelemetryFake(),
        critic,
        executor_turn=turn,
        tool_executor=tool,
        tool_policy=ToolPolicy(),
        tool_request_limit_per_round=per_round,
        tool_request_limit_total=total,
        budget_config=AgentBudgetConfig(max_rounds=1, max_duration_ms=10_000),
        execution_budget=execution_budget,
    )


@pytest.mark.asyncio
async def test_legacy_executor_port_remains_final_only_by_default() -> None:
    legacy = LegacyExecutorFake()
    service = AgentLoopService(
        ContextFake(),
        PlannerFake(),
        legacy,
        CheckpointFake(),
        PublisherFake(),
        TelemetryFake(),
        CriticFake(),
    )

    state = await service.run_once(_context(), media_id=7)

    assert state.result == _result()
    assert legacy.calls == 1


@pytest.mark.asyncio
async def test_one_allowed_tool_turn_continues_then_reaches_critic() -> None:
    turn = TurnFake([_request(), _final()])
    tool = ToolFake([_success()])
    critic = CriticFake()
    service = _service(turn, tool, critic)

    state = await service.run(_context(), media_id=7)

    assert state.round == 1
    assert state.critique is not None and state.critique.passed
    assert len(tool.calls) == 1
    assert turn.initial_calls == 1
    assert len(turn.continuation_calls) == 1
    assert turn.continuation_calls[0][0].status is ToolResultStatus.SUCCESS
    assert turn.continuation_calls[0][1] is True
    assert critic.calls == [_result()]
    call, trusted_context, _deadline = tool.calls[0]
    assert call.call_id == "tool-call-1"
    assert trusted_context.media_id == 7
    assert trusted_context.tool_calls_this_round == 0


@pytest.mark.asyncio
async def test_multiple_tool_turns_stay_inside_one_agent_round() -> None:
    turn = TurnFake([_request(), _request(), _final()])
    tool = ToolFake([_success(), _success("tool-call-2")])
    critic = CriticFake()
    service = _service(turn, tool, critic)

    state = await service.run(_context(), media_id=7)

    assert state.round == 1
    assert [call.call_id for call, _context, _deadline in tool.calls] == [
        "tool-call-1",
        "tool-call-2",
    ]
    assert [available for _result_value, available in turn.continuation_calls] == [
        True,
        True,
    ]
    assert len(critic.calls) == 1


@pytest.mark.asyncio
async def test_policy_denial_is_typed_and_does_not_execute_tool() -> None:
    turn = TurnFake([_request("shell", {}), _final()])
    tool = ToolFake([])
    critic = CriticFake()
    service = _service(turn, tool, critic)

    state = await service.run(_context(), media_id=7)

    assert state.critique is not None and state.critique.passed
    assert tool.calls == []
    denied, available = turn.continuation_calls[0]
    assert denied.status is ToolResultStatus.DENIED
    assert denied.reason_code is ToolPolicyReasonCode.TOOL_NOT_ALLOWED
    assert available is True


@pytest.mark.asyncio
async def test_tool_failure_is_reinjected_as_typed_result() -> None:
    turn = TurnFake([_request(), _final()])
    tool = ToolFake([_failed()])
    critic = CriticFake()
    service = _service(turn, tool, critic)

    state = await service.run(_context(), media_id=7)

    assert state.critique is not None and state.critique.passed
    failed, _available = turn.continuation_calls[0]
    assert failed.status is ToolResultStatus.FAILED
    assert failed.payload is None


@pytest.mark.asyncio
async def test_budget_exhaustion_denies_once_then_allows_one_disabled_continuation() -> None:
    turn = TurnFake([_request(), _request(), _final()])
    tool = ToolFake([_success()])
    critic = CriticFake()
    service = _service(turn, tool, critic, per_round=1, total=1)

    state = await service.run(_context(), media_id=7)

    assert state.critique is not None and state.critique.passed
    assert len(tool.calls) == 1
    assert [result.status for result, _available in turn.continuation_calls] == [
        ToolResultStatus.SUCCESS,
        ToolResultStatus.DENIED,
    ]
    denied, available = turn.continuation_calls[1]
    assert denied.reason_code is ToolPolicyReasonCode.BUDGET_EXHAUSTED
    assert available is False


@pytest.mark.asyncio
async def test_repeated_requests_after_exhaustion_are_bounded() -> None:
    turn = TurnFake([_request(), _request(), _request()])
    tool = ToolFake([_success()])
    service = _service(turn, tool, CriticFake(), per_round=1, total=1)

    with pytest.raises(ToolTurnBudgetExceededError):
        await service.run(_context(), media_id=7)

    assert len(tool.calls) == 1
    assert len(turn.continuation_calls) == 2
    assert turn.continuation_calls[1][0].reason_code is ToolPolicyReasonCode.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_tool_calls_share_one_active_deadline() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()
    turn = TurnFake([_request(), _request(), _final()])
    tool = ToolFake([_success(), _success("tool-call-2")], clock=clock)
    service = _service(
        turn,
        tool,
        CriticFake(),
        execution_budget=AgentExecutionBudget(monotonic=clock),
    )

    await service.run(_context(), media_id=7)

    deadlines = [deadline for _call, _context, deadline in tool.calls]
    assert deadlines[0] is not None and deadlines[1] is not None
    assert deadlines[0] > deadlines[1]

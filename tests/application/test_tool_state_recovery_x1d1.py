from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application import (
    AgentBudgetConfig,
    AgentLoopService,
    DurableToolCallState,
    DurableToolExecutionState,
    DurableToolStateLedger,
    ExecutorTurn,
    ExecutorTurnKind,
    ModelToolRequest,
    PolicyDecision,
    ToolPolicy,
    ToolPolicyReasonCode,
    ToolRecoveryIdentityMismatchError,
    ToolResult,
    ToolResultStatus,
    canonical_tool_arguments_digest,
)
from dovideo.application.tool_contracts import GetSegmentArguments
from dovideo.application.value_objects import TaskKey
from dovideo.application.checkpoint_service import AgentCheckpointService
from dovideo.infrastructure.persistence import (
    CheckpointRepository,
    InMemoryHotCheckpointCache,
    SqliteCheckpointStore,
)
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


def _context(goal: str = "explain the evidence") -> VideoContext:
    return VideoContext(
        source="memory://video",
        user_goal=goal,
        segments=(VideoSegment(start_ms=0, end_ms=60_000, transcript="opening"),),
    )


def _plan() -> AgentPlan:
    return AgentPlan(understood_goal="explain", tasks=("ground the claim",))


def _result() -> AnalysisResult:
    return AnalysisResult(
        title="grounded result",
        conclusions=("opening",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=1_000,
                source="ASR",
                content="opening",
                claim="opening",
            ),
        ),
    )


def _request(name: str = "video.get_segment") -> ExecutorTurn:
    return ExecutorTurn(
        kind=ExecutorTurnKind.TOOL_REQUEST,
        tool_request=ModelToolRequest(
            tool_name=name,
            arguments={"timestamp_ms": 1_000} if name == "video.get_segment" else {},
        ),
    )


def _final() -> ExecutorTurn:
    return ExecutorTurn(kind=ExecutorTurnKind.FINAL, final_result=_result())


def _success(call_id: str = "tool-call-1") -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name="video.get_segment",
        status=ToolResultStatus.SUCCESS,
        payload={"timestamp_ms": 1_000, "text": "opening"},
    )


class _Context:
    async def select_relevant(self, context: VideoContext, media_id: int | None = None) -> VideoContext:
        del media_id
        return context


class _Planner:
    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        del context, instruction
        return _plan()

    async def repair_plan(self, context: VideoContext, invalid_plan: AgentPlan, *, instruction: str = "") -> AgentPlan:
        del context, invalid_plan, instruction
        return _plan()

    async def replan(self, context: VideoContext, current_plan: AgentPlan, critique: CriticResult, *, instruction: str = "") -> AgentPlan:
        del context, current_plan, critique, instruction
        return _plan()


class _Critic:
    async def critique(self, context: VideoContext, plan: AgentPlan, result: AnalysisResult | None, *, instruction: str = "") -> CriticResult:
        del context, plan, result, instruction
        return CriticResult(passed=True)


class _Turns:
    def __init__(self, turns: list[ExecutorTurn], *, fail_continuation: bool = False) -> None:
        self.turns = list(turns)
        self.initial_calls = 0
        self.continuations: list[tuple[ToolResult, bool]] = []
        self.fail_continuation = fail_continuation

    async def execute_turn(self, context: VideoContext, plan: AgentPlan, previous_critique: CriticResult | None = None, *, instruction: str = "") -> ExecutorTurn:
        del context, plan, previous_critique, instruction
        self.initial_calls += 1
        return self.turns.pop(0)

    async def continue_after_tool(self, context: VideoContext, plan: AgentPlan, tool_result: ToolResult, previous_critique: CriticResult | None = None, *, instruction: str = "", tools_available: bool = True) -> ExecutorTurn:
        del context, plan, previous_critique, instruction
        self.continuations.append((tool_result, tools_available))
        if self.fail_continuation:
            raise RuntimeError("injected continuation crash")
        return self.turns.pop(0)


class _Tools:
    def __init__(self, results: list[ToolResult]) -> None:
        self.results = list(results)
        self.calls: list[object] = []

    async def execute(self, tool_call: object, trusted_context: object, remaining_deadline: float | None = None) -> ToolResult:
        del trusted_context, remaining_deadline
        self.calls.append(tool_call)
        return self.results.pop(0)


class _Checkpoint:
    def __init__(self, plan: AgentPlan | None = None, state: AgentState | None = None) -> None:
        self.plan = plan
        self.state = state
        self.saved_drafts: list[AgentState] = []

    async def load_plan(self, key: object) -> AgentPlan | None:
        del key
        return self.plan

    async def save_plan(self, key: object, plan: AgentPlan) -> None:
        del key
        self.plan = plan

    async def load_critic_state(self, key: object) -> AgentState | None:
        del key
        return self.state

    async def save_execution_state(self, key: object, state: AgentState) -> None:
        del key
        self.saved_drafts.append(state)
        self.state = state

    async def save_critic_state(self, key: object, state: AgentState) -> None:
        del key, state

    async def save_result(self, key: object, state: AgentState) -> None:
        del key
        self.state = state


class _ToolCheckpoint:
    def __init__(self, ledger: DurableToolStateLedger | None = None) -> None:
        self.ledger = ledger
        self.snapshots: list[DurableToolStateLedger] = []
        self.save_calls = 0
        self.fail_on_save: set[int] = set()
        self.fail_after_save: set[int] = set()

    async def load_tool_state(self, key: TaskKey) -> DurableToolStateLedger | None:
        del key
        return self.ledger

    async def save_tool_state(self, key: TaskKey, state: DurableToolStateLedger) -> None:
        assert state.task_key == key
        self.save_calls += 1
        if self.save_calls in self.fail_on_save:
            raise RuntimeError("injected checkpoint crash")
        self.ledger = state
        self.snapshots.append(state)
        if self.save_calls in self.fail_after_save:
            raise RuntimeError("injected post-write crash")


class _Telemetry:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount


def _service(
    turns: list[ExecutorTurn],
    tool_results: list[ToolResult],
    tool_checkpoint: _ToolCheckpoint,
    *,
    checkpoint: _Checkpoint | None = None,
    per_round: int = 4,
    total: int = 12,
    fail_continuation: bool = False,
) -> tuple[AgentLoopService, _Turns, _Tools, _Checkpoint]:
    resolved_checkpoint = checkpoint or _Checkpoint(plan=_plan())
    turn_port = _Turns(turns, fail_continuation=fail_continuation)
    tool_port = _Tools(tool_results)
    service = AgentLoopService(
        _Context(),
        _Planner(),
        object(),
        resolved_checkpoint,
        None,
        _Telemetry(),
        _Critic(),
        executor_turn=turn_port,
        tool_executor=tool_port,
        tool_checkpoint=tool_checkpoint,
        tool_policy=ToolPolicy(),
        tool_request_limit_per_round=per_round,
        tool_request_limit_total=total,
        budget_config=AgentBudgetConfig(max_rounds=1, max_duration_ms=10_000),
    )
    return service, turn_port, tool_port, resolved_checkpoint


def _key() -> TaskKey:
    return TaskKey(7, "explain the evidence", AnalysisMode.GENERAL)


def _requested(index: int = 1, *, round: int = 1) -> DurableToolCallState:
    request = ModelToolRequest(
        tool_name="video.get_segment",
        arguments={"timestamp_ms": 1_000},
    )
    return DurableToolCallState(
        task_key=_key(),
        agent_round=round,
        request_index=index,
        call_id=f"tool-call-{index}",
        tool_name=request.tool_name,
        request=request,
        execution_state=DurableToolExecutionState.REQUESTED,
    )


def _authorized(index: int = 1) -> DurableToolCallState:
    state = _requested(index)
    args = GetSegmentArguments(timestamp_ms=1_000)
    return state.transition(
        DurableToolExecutionState.AUTHORIZED,
        validated_arguments=args,
        canonical_args_digest=canonical_tool_arguments_digest(args),
        policy_decision=PolicyDecision.ALLOW,
    )


def _stored(index: int = 1, result: ToolResult | None = None) -> DurableToolCallState:
    state = _authorized(index).transition(DurableToolExecutionState.EXECUTING)
    return state.transition(
        DurableToolExecutionState.RESULT_STORED,
        tool_result=result or _success(f"tool-call-{index}"),
    )


def _ledger(*states: DurableToolCallState) -> DurableToolStateLedger:
    return DurableToolStateLedger(task_key=_key(), records=states)


@pytest.mark.parametrize(
    "left,right",
    [
        (
            GetSegmentArguments(timestamp_ms=1_000),
            {"timestamp_ms": 1_000},
        ),
    ],
)
def test_canonical_digest_uses_validated_normalized_arguments(left: object, right: object) -> None:
    assert canonical_tool_arguments_digest(left) == canonical_tool_arguments_digest(right)  # type: ignore[arg-type]


def test_durable_state_machine_distinguishes_execution_facts() -> None:
    state = _authorized()
    executing = state.transition(DurableToolExecutionState.EXECUTING)
    assert executing.execution_state is DurableToolExecutionState.EXECUTING
    stored = executing.transition(DurableToolExecutionState.RESULT_STORED, tool_result=_success())
    assert stored.tool_result == _success()
    consumed = stored.transition(DurableToolExecutionState.CONSUMED)
    assert consumed.result_consumed is True


@pytest.mark.asyncio
async def test_requested_recovery_reuses_request_without_initial_executor() -> None:
    store = _ToolCheckpoint(_ledger(_requested()))
    service, turns, tools, _checkpoint = _service([_final()], [_success()], store)
    state = await service.run_once(_context(), media_id=7)
    assert state.result == _result()
    assert turns.initial_calls == 0
    assert len(tools.calls) == 1
    assert tools.calls[0].call_id == "tool-call-1"  # type: ignore[attr-defined]
    assert store.ledger is not None
    assert store.ledger.latest_for_round(1).execution_state is DurableToolExecutionState.CONSUMED


@pytest.mark.asyncio
async def test_failure_before_requested_save_leaves_no_recoverable_call() -> None:
    store = _ToolCheckpoint()
    store.fail_on_save.add(1)
    service, _turns, tools, _checkpoint = _service([_request()], [_success()], store)
    with pytest.raises(RuntimeError, match="injected checkpoint crash"):
        await service.run_once(_context(), media_id=7)
    assert store.ledger is None
    assert tools.calls == []


@pytest.mark.asyncio
async def test_failure_after_requested_save_recovers_same_call_id() -> None:
    store = _ToolCheckpoint()
    store.fail_on_save.add(2)
    service, _turns, tools, _checkpoint = _service([_request()], [_success()], store)
    with pytest.raises(RuntimeError, match="injected checkpoint crash"):
        await service.run_once(_context(), media_id=7)
    assert store.ledger is not None
    assert store.ledger.latest_for_round(1).execution_state is DurableToolExecutionState.REQUESTED
    resumed, resumed_turns, resumed_tools, _ = _service([_final()], [_success()], store)
    await resumed.run_once(_context(), media_id=7)
    assert resumed_turns.initial_calls == 0
    assert resumed_tools.calls[0].call_id == "tool-call-1"  # type: ignore[attr-defined]
    assert tools.calls == []


@pytest.mark.asyncio
async def test_failure_before_result_save_recovers_executing_call() -> None:
    store = _ToolCheckpoint()
    store.fail_on_save.add(4)
    service, _turns, tools, _checkpoint = _service([_request()], [_success()], store)
    with pytest.raises(RuntimeError, match="injected checkpoint crash"):
        await service.run_once(_context(), media_id=7)
    assert store.ledger is not None
    assert store.ledger.latest_for_round(1).execution_state is DurableToolExecutionState.EXECUTING
    resumed, resumed_turns, resumed_tools, _ = _service([_final()], [_success()], store)
    await resumed.run_once(_context(), media_id=7)
    assert resumed_turns.initial_calls == 0
    assert resumed_tools.calls[0].call_id == "tool-call-1"  # type: ignore[attr-defined]
    assert tools.calls[0].call_id == "tool-call-1"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_authorized_recovery_keeps_call_id_and_arguments() -> None:
    store = _ToolCheckpoint(_ledger(_authorized()))
    service, turns, tools, _checkpoint = _service([_final()], [_success()], store)
    await service.run_once(_context(), media_id=7)
    assert turns.initial_calls == 0
    assert tools.calls[0].call_id == "tool-call-1"  # type: ignore[attr-defined]
    assert tools.calls[0].validated_arguments.timestamp_ms == 1_000  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_executing_without_result_reexecutes_read_only_call_at_least_once() -> None:
    executing = _authorized().transition(DurableToolExecutionState.EXECUTING)
    store = _ToolCheckpoint(_ledger(executing))
    service, _turns, tools, _checkpoint = _service([_final()], [_success()], store)
    await service.run_once(_context(), media_id=7)
    assert len(tools.calls) == 1
    assert tools.calls[0].call_id == "tool-call-1"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_result_stored_recovery_deduplicates_tool_execution() -> None:
    store = _ToolCheckpoint(_ledger(_stored()))
    service, turns, tools, _checkpoint = _service([_final()], [], store)
    await service.run_once(_context(), media_id=7)
    assert turns.initial_calls == 0
    assert tools.calls == []
    assert turns.continuations[0][0] == _success()


@pytest.mark.asyncio
async def test_continuation_failure_reuses_durable_result_without_tool_retry() -> None:
    store = _ToolCheckpoint()
    service, _turns, tools, _checkpoint = _service(
        [_request()],
        [_success()],
        store,
        fail_continuation=True,
    )
    with pytest.raises(RuntimeError, match="injected continuation crash"):
        await service.run_once(_context(), media_id=7)
    assert len(tools.calls) == 1
    resumed, _resumed_turns, resumed_tools, _ = _service([_final()], [], store)
    await resumed.run_once(_context(), media_id=7)
    assert resumed_tools.calls == []


@pytest.mark.asyncio
async def test_denied_result_is_reused_without_budget_double_count() -> None:
    requested = _requested()
    denied = requested.transition(
        DurableToolExecutionState.DENIED,
        policy_decision=PolicyDecision.DENY,
        policy_reason=ToolPolicyReasonCode.TOOL_NOT_ALLOWED,
    )
    denied_result = ToolResult(
        call_id="tool-call-1",
        tool_name="video.get_segment",
        status=ToolResultStatus.DENIED,
        reason_code=ToolPolicyReasonCode.TOOL_NOT_ALLOWED,
    )
    store = _ToolCheckpoint(_ledger(denied.transition(DurableToolExecutionState.RESULT_STORED, tool_result=denied_result)))
    service, _turns, tools, _checkpoint = _service([_final()], [], store, total=1)
    await service.run_once(_context(), media_id=7)
    assert tools.calls == []
    assert store.ledger is not None and store.ledger.request_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ToolResultStatus.FAILED, ToolResultStatus.TRUNCATED])
async def test_failed_and_truncated_results_are_reused_exactly(status: ToolResultStatus) -> None:
    result = ToolResult(
        call_id="tool-call-1",
        tool_name="video.get_segment",
        status=status,
        payload={"partial": True} if status is ToolResultStatus.TRUNCATED else None,
        truncated=status is ToolResultStatus.TRUNCATED,
    )
    store = _ToolCheckpoint(_ledger(_stored(result=result)))
    service, _turns, tools, _checkpoint = _service([_final()], [], store)
    await service.run_once(_context(), media_id=7)
    assert tools.calls == []
    assert store.ledger is not None
    assert store.ledger.latest_for_round(1).tool_result == result


@pytest.mark.asyncio
async def test_next_request_is_durable_before_previous_consumed() -> None:
    store = _ToolCheckpoint()
    service, turns, tools, _checkpoint = _service([_request(), _request(), _final()], [_success(), _success("tool-call-2")], store)
    await service.run_once(_context(), media_id=7)
    assert [call.call_id for call in tools.calls] == ["tool-call-1", "tool-call-2"]  # type: ignore[attr-defined]
    assert turns.initial_calls == 1
    assert store.ledger is not None
    assert [record.execution_state for record in store.ledger.records] == [
        DurableToolExecutionState.CONSUMED,
        DurableToolExecutionState.CONSUMED,
    ]


@pytest.mark.asyncio
async def test_partial_next_request_write_recovers_latest_request_without_replaying_previous() -> None:
    store = _ToolCheckpoint()
    first = _service([_request(), _request(), _final()], [_success(), _success("tool-call-2")], store)
    service, _turns, tools, _checkpoint = first
    store.fail_on_save.add(6)
    with pytest.raises(RuntimeError, match="injected checkpoint crash"):
        await service.run_once(_context(), media_id=7)
    assert store.ledger is not None
    assert store.ledger.latest_for_round(1).request_index == 2
    resumed, resumed_turns, resumed_tools, _ = _service([_final()], [_success("tool-call-2")], store)
    await resumed.run_once(_context(), media_id=7)
    assert resumed_turns.initial_calls == 0
    assert [call.call_id for call in tools.calls] == ["tool-call-1"]  # type: ignore[attr-defined]
    assert [call.call_id for call in resumed_tools.calls] == ["tool-call-2"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_final_draft_precedence_survives_consumed_write_failure() -> None:
    store = _ToolCheckpoint()
    service, _turns, _tools, checkpoint = _service([_request(), _final()], [_success()], store)
    store.fail_after_save.add(5)
    with pytest.raises(RuntimeError, match="injected post-write crash"):
        await service.run_once(_context(), media_id=7)
    assert checkpoint.saved_drafts
    resumed, resumed_turns, resumed_tools, resumed_checkpoint = _service([], [], store, checkpoint=checkpoint)
    resumed_state = await resumed.run(_context(), media_id=7)
    assert resumed_state.critique is not None and resumed_state.critique.passed
    assert resumed_turns.initial_calls == 0
    assert resumed_tools.calls == []
    assert resumed_checkpoint.saved_drafts == checkpoint.saved_drafts


@pytest.mark.asyncio
async def test_budget_recovery_preserves_round_and_total_counts() -> None:
    first = _stored()
    second = _requested(2)
    store = _ToolCheckpoint(_ledger(first, second))
    service, _turns, tools, _checkpoint = _service([_final()], [_success("tool-call-2")], store, per_round=1, total=1)
    await service.run_once(_context(), media_id=7)
    assert tools.calls == []
    assert store.ledger is not None and store.ledger.request_count == 2


@pytest.mark.asyncio
async def test_trusted_identity_mismatch_fails_closed_before_tool() -> None:
    wrong_key = TaskKey(99, "other goal", AnalysisMode.GENERAL)
    state = _requested()
    wrong = state.model_validate({**state.model_dump(mode="python"), "task_key": wrong_key})
    store = _ToolCheckpoint(DurableToolStateLedger(task_key=wrong_key, records=(wrong,)))
    service, turns, tools, _checkpoint = _service([_final()], [], store)
    with pytest.raises(ToolRecoveryIdentityMismatchError):
        await service.run_once(_context(), media_id=7)
    assert turns.initial_calls == 0
    assert tools.calls == []


@pytest.mark.asyncio
async def test_terminal_checkpoint_outranks_stale_tool_state() -> None:
    terminal = AgentState(goal=_key().goal, plan=_plan(), result=_result(), critique=CriticResult(passed=True), round=1)
    store = _ToolCheckpoint(_ledger(_authorized()))
    service, turns, tools, _checkpoint = _service([], [], store, checkpoint=_Checkpoint(plan=_plan(), state=terminal))
    result = await service.run(_context(), media_id=7)
    assert result == terminal
    assert turns.initial_calls == 0
    assert tools.calls == []


@pytest.mark.asyncio
async def test_draft_checkpoint_outranks_stale_tool_state_and_goes_to_critic() -> None:
    draft = AgentState(goal=_key().goal, plan=_plan(), result=_result(), critique=None, round=1)
    store = _ToolCheckpoint(_ledger(_authorized()))
    service, turns, tools, _checkpoint = _service([], [], store, checkpoint=_Checkpoint(plan=_plan(), state=draft))
    result = await service.run(_context(), media_id=7)
    assert result.critique is not None and result.critique.passed
    assert turns.initial_calls == 0
    assert tools.calls == []


@pytest.mark.asyncio
async def test_requested_policy_recovery_is_deterministic_and_does_not_rebuild_media() -> None:
    store = _ToolCheckpoint(_ledger(_requested()))
    service, _turns, tools, _checkpoint = _service([_final()], [_success()], store)
    context = _context()
    await service.run_once(context, media_id=7)
    assert tools.calls[0].call_id == "tool-call-1"  # type: ignore[attr-defined]
    assert tools.calls[0].validated_arguments.timestamp_ms == 1_000  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_existing_checkpoint_repository_round_trips_tool_ledger(tmp_path) -> None:
    durable = SqliteCheckpointStore(tmp_path / "checkpoints.sqlite3")
    repository = CheckpointRepository(durable, InMemoryHotCheckpointCache())
    service = AgentCheckpointService(repository)
    key = _key()
    ledger = _ledger(_requested())
    await service.save_tool_state(key, ledger)
    loaded = await service.load_tool_state(key)
    assert loaded == ledger
    assert loaded is not None and loaded.records[0].call_id == "tool-call-1"
    assert durable.records(7)[0].checkpoint_name.endswith(":toolState")
    durable.close()

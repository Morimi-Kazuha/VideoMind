from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from dovideo.application import (
    AgentBudgetConfig,
    AgentExecutionBudget,
    AgentLoopService,
    BudgetExceededError,
    BudgetUsage,
    DeadlineExceededError,
    InMemoryAgentBudgetUsage,
    InvalidBudgetError,
)
from dovideo.domain import CriticResult

from tests.application.test_agent_loop_7e import (
    CheckpointFake,
    ContextFake,
    CriticFake,
    ExecutorFake,
    PlannerFake,
    PublisherFake,
    TelemetryFake,
    _context,
    _result,
)


def _build(
    *,
    budget: AgentBudgetConfig | dict[str, object] | None = None,
    usage: object | None = None,
    execution_budget: object | None = None,
    critiques: list | None = None,
    results: list | None = None,
    state=None,
):
    timeline: list[tuple[object, ...]] = []
    context = ContextFake(timeline)
    planner = PlannerFake(timeline)
    executor = ExecutorFake(timeline, results or [_result()])
    critic = CriticFake(timeline, critiques or [])
    checkpoint = CheckpointFake(timeline, state=state)
    publisher = PublisherFake(timeline)
    telemetry = TelemetryFake()
    service = AgentLoopService(
        context,
        planner,
        executor,
        checkpoint,
        publisher,
        telemetry,
        critic,
        budget_config=budget,
        usage_source=usage,
        execution_budget=execution_budget,
    )
    return service, context, planner, executor, critic, checkpoint, telemetry


@pytest.mark.parametrize(
    "field",
    ["maxRounds", "maxDurationMs", "maxEstimatedTokens"],
)
def test_runtime_budget_requires_positive_round_duration_and_token_caps(field: str) -> None:
    config = {field: 0}
    with pytest.raises(InvalidBudgetError):
        _build(budget=config)


def test_usage_tracker_validates_finite_values_and_adds_provider_deltas() -> None:
    tracker = InMemoryAgentBudgetUsage()
    assert tracker.record(BudgetUsage(estimatedTokens=2, estimatedCost=0.1)).estimated_tokens == 2
    assert tracker.add(3, 0.2).model_dump(by_alias=True) == {
        "estimatedTokens": 5,
        "estimatedCost": pytest.approx(0.3),
    }
    with pytest.raises(InvalidBudgetError):
        tracker.add(float("inf"), 0)
    with pytest.raises(InvalidBudgetError):
        tracker.record(estimated_cost=float("nan"))


@pytest.mark.asyncio
async def test_exact_token_cap_is_allowed_but_overage_terminates_once() -> None:
    exact_usage = InMemoryAgentBudgetUsage(BudgetUsage(estimatedTokens=10))
    service, _context_fake, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1, "maxEstimatedTokens": 10},
        usage=exact_usage,
        critiques=[{"passed": True}],
    )
    await service.run(_context(), media_id=7)
    assert telemetry.counts["budgetTerminations"] == 0

    over_usage = InMemoryAgentBudgetUsage(BudgetUsage(estimatedTokens=11))
    service, _context_fake, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1, "maxEstimatedTokens": 10},
        usage=over_usage,
        critiques=[{"passed": True}],
    )
    with pytest.raises(BudgetExceededError, match="Token"):
        await service.run(_context(), media_id=7)
    assert telemetry.counts["budgetTerminations"] == 1


@pytest.mark.asyncio
async def test_zero_cost_cap_disables_cost_termination() -> None:
    usage = InMemoryAgentBudgetUsage(BudgetUsage(estimatedTokens=0, estimatedCost=999.0))
    service, _context_fake, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1, "maxEstimatedCost": 0},
        usage=usage,
        critiques=[{"passed": True}],
    )
    await service.run(_context(), media_id=7)
    assert telemetry.counts["budgetTerminations"] == 0


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_nested_deadline_keeps_earlier_scope_and_restores_on_close() -> None:
    clock = _FakeClock()
    budget = AgentExecutionBudget(monotonic=clock)
    outer = budget.open(100)
    assert budget.remaining_ms() == 100
    inner = budget.open(10)
    assert budget.remaining_ms() == 10
    clock.value = 0.011
    with pytest.raises(DeadlineExceededError, match="Agent 已耗尽"):
        budget.check("nested")
    inner.close()
    clock.value = 0.011
    assert budget.remaining_ms() == 89
    outer.close()
    assert budget.remaining_ms() is None


@pytest.mark.asyncio
async def test_long_context_timeout_cancels_call_and_wraps_budget_error() -> None:
    clock = _FakeClock()
    execution_budget = AgentExecutionBudget(monotonic=clock)
    service, context, _planner, executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1, "maxDurationMs": 10},
        execution_budget=execution_budget,
        critiques=[{"passed": True}],
    )

    cancelled = False

    async def slow_select(value, media_id=None):
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return value

    context.select_relevant = slow_select
    with pytest.raises(BudgetExceededError) as raised:
        await service.run(_context(), media_id=7)
    assert isinstance(raised.value.__cause__, DeadlineExceededError)
    assert cancelled
    assert executor.calls == []
    assert telemetry.counts["budgetTerminations"] == 1


@pytest.mark.asyncio
async def test_ordinary_provider_failure_is_not_reclassified_as_budget() -> None:
    service, context, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[{"passed": True}],
    )

    async def fail_select(value, media_id=None):
        raise ValueError("provider failed")

    context.select_relevant = fail_select
    with pytest.raises(ValueError, match="provider failed"):
        await service.run(_context(), media_id=7)
    assert telemetry.counts["budgetTerminations"] == 0


@pytest.mark.asyncio
async def test_provider_local_timeout_is_not_reclassified_as_deadline() -> None:
    service, context, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[{"passed": True}],
    )

    async def provider_timeout(value, media_id=None):
        raise TimeoutError("provider timeout")

    context.select_relevant = provider_timeout
    with pytest.raises(TimeoutError, match="provider timeout"):
        await service.run(_context(), media_id=7)
    assert telemetry.counts["budgetTerminations"] == 0


@pytest.mark.asyncio
async def test_replan_provider_timeout_falls_back_to_old_plan() -> None:
    service, _context_fake, planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[{"passed": True}],
    )
    old_plan = planner.planned

    async def provider_replan_timeout(*args, **kwargs):
        raise asyncio.TimeoutError("planner provider timeout")

    planner.replan = provider_replan_timeout
    returned = await service.revise_plan_for_retry(
        7,
        _context(),
        old_plan,
        CriticResult(passed=False, missingRequirements=["missing"]),
    )
    assert returned == old_plan
    assert telemetry.counts["planRevisionFallbacks"] == 1
    assert telemetry.counts["budgetTerminations"] == 0


@pytest.mark.asyncio
async def test_runtime_error_from_deadline_is_not_swallowed_by_replan() -> None:
    service, _context_fake, planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[{"passed": True}],
    )

    async def wrapped_deadline(*args, **kwargs):
        try:
            raise DeadlineExceededError("inner deadline")
        except DeadlineExceededError as error:
            raise RuntimeError("planner wrapper") from error

    planner.replan = wrapped_deadline
    with pytest.raises(RuntimeError, match="planner wrapper"):
        await service.revise_plan_for_retry(
            7,
            _context(),
            planner.planned,
            CriticResult(passed=False, missingRequirements=["missing"]),
        )
    assert telemetry.counts["planRevisionFallbacks"] == 0
    assert telemetry.counts["budgetTerminations"] == 0


@pytest.mark.asyncio
async def test_runtime_error_from_deadline_is_wrapped_once_by_run() -> None:
    service, context, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[{"passed": True}],
    )

    async def wrapped_deadline(value, media_id=None):
        try:
            raise DeadlineExceededError("wrapped deadline")
        except DeadlineExceededError as error:
            raise RuntimeError("context wrapper") from error

    context.select_relevant = wrapped_deadline
    with pytest.raises(BudgetExceededError) as raised:
        await service.run(_context(), media_id=7)
    assert raised.value.args == ("wrapped deadline",)
    assert str(raised.value) == "wrapped deadline"
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert telemetry.counts["budgetTerminations"] == 1


@pytest.mark.asyncio
async def test_direct_deadline_wrapper_has_one_message_and_original_cause() -> None:
    service, context, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[{"passed": True}],
    )

    async def direct_deadline(value, media_id=None):
        raise DeadlineExceededError("direct deadline")

    context.select_relevant = direct_deadline
    with pytest.raises(BudgetExceededError) as raised:
        await service.run(_context(), media_id=7)
    assert raised.value.args == ("direct deadline",)
    assert str(raised.value) == "direct deadline"
    assert isinstance(raised.value.__cause__, DeadlineExceededError)
    assert telemetry.counts["budgetTerminations"] == 1


@pytest.mark.asyncio
async def test_executor_draft_is_saved_before_post_call_budget_termination() -> None:
    usage = InMemoryAgentBudgetUsage()
    service, _context_fake, _planner, executor, critic, checkpoint, telemetry = _build(
        budget={"maxRounds": 1, "maxEstimatedTokens": 10},
        usage=usage,
        critiques=[{"passed": True}],
    )

    original_execute = executor.execute

    async def execute_and_exhaust(*args, **kwargs):
        result = await original_execute(*args, **kwargs)
        usage.record(estimated_tokens=11)
        return result

    executor.execute = execute_and_exhaust
    with pytest.raises(BudgetExceededError, match="Token"):
        await service.run(_context(), media_id=7)
    assert len(checkpoint.saved_drafts) == 1
    assert critic.calls == []
    assert telemetry.counts["budgetTerminations"] == 1


@pytest.mark.asyncio
async def test_saved_draft_can_resume_with_a_fresh_budget_without_executor() -> None:
    usage = InMemoryAgentBudgetUsage()
    service, _context_fake, _planner, executor, _critic, checkpoint, _telemetry = _build(
        budget={"maxRounds": 1, "maxEstimatedTokens": 10},
        usage=usage,
        critiques=[{"passed": True}],
    )
    original_execute = executor.execute

    async def execute_and_exhaust(*args, **kwargs):
        result = await original_execute(*args, **kwargs)
        usage.record(estimated_tokens=11)
        return result

    executor.execute = execute_and_exhaust
    with pytest.raises(BudgetExceededError):
        await service.run(_context(), media_id=7)
    checkpoint.state = checkpoint.saved_drafts[-1][1]

    fresh_usage = InMemoryAgentBudgetUsage()
    resumed, _context_fake, _planner, resumed_executor, resumed_critic, _checkpoint, _telemetry = _build(
        budget={"maxRounds": 1, "maxEstimatedTokens": 10},
        usage=fresh_usage,
        critiques=[{"passed": True}],
        state=checkpoint.state,
    )
    resumed._checkpoint = checkpoint
    returned = await resumed.run(_context(), media_id=7)
    assert returned.critique is not None and returned.critique.passed
    assert resumed_executor.calls == []
    assert len(resumed_critic.calls) == 1


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_is_not_budget_termination() -> None:
    service, context, _planner, _executor, _critic, _checkpoint, telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[{"passed": True}],
    )

    async def cancel_select(value, media_id=None):
        raise asyncio.CancelledError()

    context.select_relevant = cancel_select
    with pytest.raises(asyncio.CancelledError):
        await service.run(_context(), media_id=7)
    assert telemetry.counts["budgetTerminations"] == 0

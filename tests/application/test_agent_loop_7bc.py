from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application import AgentLoopService
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    ModeProfile,
    TaskStage,
    VideoContext,
    VideoSegment,
)


def _context(goal: str = "goal") -> VideoContext:
    return VideoContext(
        source="video.mp4",
        user_goal=goal,
        segments=[VideoSegment(startMs=0, endMs=60_000, transcript="evidence")],
    )


def _plan(task: str = "task") -> AgentPlan:
    return AgentPlan(understoodGoal="goal", tasks=[task])


def _result() -> AnalysisResult:
    return AnalysisResult(title="title", conclusions=["conclusion"])


class ContextFake:
    def __init__(self, calls: list[tuple[object, ...]], selected: VideoContext | None = None) -> None:
        self.calls = calls
        self.selected = selected or _context()

    async def select_relevant(self, context: VideoContext, media_id: int | None = None) -> VideoContext:
        self.calls.append(("select", context, media_id))
        return self.selected


class CheckpointFake:
    def __init__(self, calls: list[tuple[object, ...]], plan: AgentPlan | None = None) -> None:
        self.calls = calls
        self.plan = plan

    async def load_plan(self, key: object) -> AgentPlan | None:
        self.calls.append(("load_plan", key))
        return self.plan

    async def save_plan(self, key: object, plan: AgentPlan) -> None:
        self.calls.append(("save_plan", key, plan))

    async def save_execution_state(self, key: object, state: AgentState) -> None:
        self.calls.append(("save_draft", key, state))


class PublisherFake:
    def __init__(self, calls: list[tuple[object, ...]]) -> None:
        self.calls = calls

    async def publish(self, key: object, event: object) -> None:
        self.calls.append(("event", key, event))


class TelemetryFake:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount


class PlannerFake:
    def __init__(self, calls: list[tuple[object, ...]], planned: AgentPlan, repaired: AgentPlan) -> None:
        self.calls = calls
        self.planned = planned
        self.repaired = repaired

    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        self.calls.append(("plan", context, instruction))
        return self.planned

    async def repair_plan(
        self,
        context: VideoContext,
        invalid_plan: AgentPlan,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        self.calls.append(("repair", context, invalid_plan, instruction))
        return self.repaired


class ExecutorFake:
    def __init__(self, calls: list[tuple[object, ...]], result: AnalysisResult | None = None) -> None:
        self.calls = calls
        self.result = result or _result()

    async def execute(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> AnalysisResult:
        self.calls.append(("execute", context, plan, previous_critique, instruction))
        return self.result


def _service(
    calls: list[tuple[object, ...]],
    *,
    checkpoint_plan: AgentPlan | None = None,
    planned: AgentPlan | None = None,
    repaired: AgentPlan | None = None,
) -> tuple[AgentLoopService, PlannerFake, ExecutorFake, CheckpointFake, TelemetryFake]:
    telemetry = TelemetryFake()
    checkpoint = CheckpointFake(calls, checkpoint_plan)
    planner = PlannerFake(calls, planned or _plan(), repaired or _plan("repaired"))
    executor = ExecutorFake(calls)
    service = AgentLoopService(
        ContextFake(calls), planner, executor, checkpoint,
        PublisherFake(calls), telemetry,
    )
    return service, planner, executor, checkpoint, telemetry


@pytest.mark.asyncio
async def test_plan_then_executor_order_checkpoint_events_and_instructions() -> None:
    calls: list[tuple[object, ...]] = []
    service, planner, executor, checkpoint, _telemetry = _service(calls)
    profile = ModeProfile(
        mode=AnalysisMode.LEARNING,
        planInstruction=" plan instruction ",
        executeInstruction=" execute instruction ",
    )

    state = await service.run_once(_context(), media_id=7, profile=profile)

    names = [call[0] for call in calls]
    assert names == [
        "select",
        "load_plan",
        "plan",
        "save_plan",
        "event",
        "event",
        "execute",
        "save_draft",
        "event",
    ]
    planner_call = next(item for item in calls if item[0] == "plan")
    executor_call = next(item for item in calls if item[0] == "execute")
    assert planner_call[2] == " plan instruction "
    assert executor_call[4] == " execute instruction "
    assert state.result is not None
    assert state.critique is None
    assert state.round == 1
    load_call = next(item for item in calls if item[0] == "load_plan")
    assert load_call[1].mode is AnalysisMode.LEARNING
    assert calls[4][2].stage is TaskStage.PLAN_COMPLETED
    assert calls[5][2].stage is TaskStage.EXECUTOR_STARTED
    assert calls[8][2].stage is TaskStage.EXECUTOR_COMPLETED


@pytest.mark.asyncio
async def test_checkpoint_plan_precedes_saved_state_and_planner() -> None:
    calls: list[tuple[object, ...]] = []
    cached = _plan("cached")
    service, planner, _executor, checkpoint, _telemetry = _service(
        calls, checkpoint_plan=cached, planned=_plan("planner")
    )
    saved = AgentState(goal="goal", plan=_plan("saved"))

    _relevant, resolved = await service.prepare_plan(
        _context(), media_id=3, saved_state=saved
    )

    assert resolved == cached
    assert [item[0] for item in calls] == ["select", "load_plan", "event"]
    assert not any(item[0] == "plan" for item in calls)
    assert not any(item[0] == "save_plan" for item in checkpoint.calls)


@pytest.mark.asyncio
async def test_invalid_plan_is_repaired_once_and_invalid_repair_fails() -> None:
    calls: list[tuple[object, ...]] = []
    invalid = _plan("😀" * 251)
    telemetry = TelemetryFake()
    planner = PlannerFake(calls, invalid, invalid)
    service = AgentLoopService(
        ContextFake(calls), planner, ExecutorFake(calls),
        CheckpointFake(calls), PublisherFake(calls), telemetry,
    )

    with pytest.raises(ValueError, match="Planner"):
        await service.run_once(_context(), media_id=9)

    assert [item[0] for item in calls].count("repair") == 1
    assert telemetry.counts["planStructureRepairs"] == 1
    assert not any(item[0] == "save_plan" for item in calls)


@pytest.mark.asyncio
async def test_none_profile_uses_general_empty_instructions_and_no_media_side_effects() -> None:
    calls: list[tuple[object, ...]] = []
    service, planner, executor, checkpoint, _telemetry = _service(calls)

    state = await service.run_once(_context(), profile=None)

    planner_call = next(item for item in calls if item[0] == "plan")
    executor_call = next(item for item in calls if item[0] == "execute")
    assert planner_call[2] == ""
    assert executor_call[4] == ""
    assert state.round == 1
    assert not any(item[0] in {"load_plan", "save_plan", "save_draft", "event"} for item in calls)


@pytest.mark.asyncio
async def test_execute_round_forwards_previous_critique_and_requested_round() -> None:
    calls: list[tuple[object, ...]] = []
    service, _planner, executor, _checkpoint, _telemetry = _service(calls)
    critique = CriticResult(passed=False, feedback=["retry"])

    state = await service.execute_round(
        _context(), _plan(), media_id=12, previous_critique=critique, round=2
    )

    executor_call = next(item for item in calls if item[0] == "execute")
    assert executor_call[3] == critique
    assert state.round == 2
    assert state.result is not None
    assert state.critique is None


def test_context_validation_and_draft_resume_predicate_are_explicit() -> None:
    calls: list[tuple[object, ...]] = []
    service, _planner, _executor, _checkpoint, _telemetry = _service(calls)
    with pytest.raises(ValueError, match="Agent"):
        service.validate_context(VideoContext(source="video.mp4", user_goal="goal"))

    draft = AgentState(goal="goal", plan=_plan(), result=_result(), round=1)
    assert service.can_resume_from_draft(draft)
    assert not service.can_resume_from_draft(
        AgentState(goal="goal", plan=_plan(), result=_result(), critique=CriticResult(), round=1)
    )

from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application import AgentLoopService, InvalidBudgetError
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    CriticResult,
    ModeProfile,
    TaskStage,
    VideoContext,
    VideoSegment,
)


def _context(goal: str = "goal", *, transcript: str = "the claim") -> VideoContext:
    return VideoContext(
        source="video.mp4",
        user_goal=goal,
        segments=[VideoSegment(startMs=0, endMs=60_000, transcript=transcript)],
    )


def _plan(name: str = "task") -> AgentPlan:
    return AgentPlan(understoodGoal="goal", tasks=[name])


def _result(name: str = "claim") -> AnalysisResult:
    return AnalysisResult(
        title="title",
        conclusions=[name],
        evidence=[
            AnalysisEvidence(
                timestampMs=1_000,
                source="ASR",
                content="the claim",
                claim=name,
            )
        ],
    )


class ContextFake:
    def __init__(self, timeline: list[tuple[object, ...]], refined: VideoContext | None = None) -> None:
        self.timeline = timeline
        self.refined = refined
        self.select_calls: list[tuple[VideoContext, int | None]] = []
        self.refine_calls: list[tuple[int | None, VideoContext, VideoContext, CriticResult]] = []

    async def select_relevant(self, context: VideoContext, media_id: int | None = None) -> VideoContext:
        self.timeline.append(("select", media_id))
        self.select_calls.append((context, media_id))
        return context

    async def refine_for_critique(
        self,
        media_id: int | None,
        full_context: VideoContext,
        selected_context: VideoContext,
        critique: CriticResult,
    ) -> VideoContext:
        self.timeline.append(("refine", critique))
        self.refine_calls.append((media_id, full_context, selected_context, critique))
        return self.refined or selected_context


class PlannerFake:
    def __init__(
        self,
        timeline: list[tuple[object, ...]],
        *,
        planned: AgentPlan | None = None,
        replanned: AgentPlan | None = None,
        replan_error: Exception | None = None,
    ) -> None:
        self.timeline = timeline
        self.planned = planned or _plan()
        self.replanned = replanned or _plan("replanned")
        self.replan_error = replan_error
        self.plan_calls: list[tuple[VideoContext, str]] = []
        self.replan_calls: list[tuple[VideoContext, AgentPlan, CriticResult, str]] = []

    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        self.timeline.append(("plan", instruction))
        self.plan_calls.append((context, instruction))
        return self.planned

    async def repair_plan(
        self,
        context: VideoContext,
        invalid_plan: AgentPlan,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        self.timeline.append(("repair", instruction))
        return self.replanned

    async def replan(
        self,
        context: VideoContext,
        current_plan: AgentPlan,
        critique: CriticResult,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        self.timeline.append(("replan", current_plan, critique, instruction))
        self.replan_calls.append((context, current_plan, critique, instruction))
        if self.replan_error is not None:
            raise self.replan_error
        return self.replanned


class ExecutorFake:
    def __init__(self, timeline: list[tuple[object, ...]], results: list[AnalysisResult]) -> None:
        self.timeline = timeline
        self.results = list(results)
        self.calls: list[tuple[VideoContext, AgentPlan, CriticResult | None, str]] = []

    async def execute(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> AnalysisResult:
        self.timeline.append(("execute", plan, previous_critique, instruction))
        self.calls.append((context, plan, previous_critique, instruction))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class CriticFake:
    def __init__(self, timeline: list[tuple[object, ...]], critiques: list[CriticResult | None]) -> None:
        self.timeline = timeline
        self.critiques = list(critiques)
        self.calls: list[tuple[VideoContext, AgentPlan, AnalysisResult | None, str]] = []

    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult | None,
        *,
        instruction: str = "",
    ) -> CriticResult | None:
        self.timeline.append(("critique", plan, result, instruction))
        self.calls.append((context, plan, result, instruction))
        return self.critiques.pop(0) if len(self.critiques) > 1 else self.critiques[0]


class CheckpointFake:
    def __init__(
        self,
        timeline: list[tuple[object, ...]],
        *,
        state: AgentState | None = None,
        plan: AgentPlan | None = None,
    ) -> None:
        self.timeline = timeline
        self.state = state
        self.plan = plan
        self.load_state_calls: list[object] = []
        self.load_plan_calls: list[object] = []
        self.saved_plans: list[tuple[object, AgentPlan]] = []
        self.saved_drafts: list[tuple[object, AgentState]] = []
        self.saved_critics: list[tuple[object, AgentState]] = []
        self.saved_results: list[tuple[object, AgentState]] = []

    async def load_critic_state(self, key: object) -> AgentState | None:
        self.timeline.append(("load_state", key))
        self.load_state_calls.append(key)
        return self.state

    async def load_plan(self, key: object) -> AgentPlan | None:
        self.timeline.append(("load_plan", key))
        self.load_plan_calls.append(key)
        return self.plan

    async def save_plan(self, key: object, plan: AgentPlan) -> None:
        self.timeline.append(("save_plan", key, plan))
        self.saved_plans.append((key, plan))

    async def save_execution_state(self, key: object, state: AgentState) -> None:
        self.timeline.append(("save_draft", key, state))
        self.saved_drafts.append((key, state))

    async def save_critic_state(self, key: object, state: AgentState) -> None:
        self.timeline.append(("save_critic", key, state))
        self.saved_critics.append((key, state))

    async def save_result(self, key: object, state: AgentState) -> None:
        self.timeline.append(("save_result", key, state))
        self.saved_results.append((key, state))


class PublisherFake:
    def __init__(self, timeline: list[tuple[object, ...]]) -> None:
        self.timeline = timeline
        self.events: list[tuple[object, object]] = []

    async def publish(self, key: object, event: object) -> None:
        self.timeline.append(("event", event.stage, event.message, key))
        self.events.append((key, event))


class TelemetryFake:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def increment(self, metric: str, amount: int = 1, **_: object) -> None:
        self.counts[metric] += amount


def _service(
    critiques: list[CriticResult | None],
    *,
    max_rounds: int = 2,
    state: AgentState | None = None,
    checkpoint_plan: AgentPlan | None = None,
    planner: PlannerFake | None = None,
    results: list[AnalysisResult] | None = None,
) -> tuple[
    AgentLoopService,
    ContextFake,
    PlannerFake,
    ExecutorFake,
    CriticFake,
    CheckpointFake,
    PublisherFake,
    TelemetryFake,
    list[tuple[object, ...]],
]:
    timeline: list[tuple[object, ...]] = []
    context = ContextFake(timeline)
    planner = planner or PlannerFake(timeline)
    executor = ExecutorFake(timeline, results or [_result()])
    critic = CriticFake(timeline, critiques)
    checkpoint = CheckpointFake(timeline, state=state, plan=checkpoint_plan)
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
        budget_config={"maxRounds": max_rounds},
    )
    return service, context, planner, executor, critic, checkpoint, publisher, telemetry, timeline


@pytest.mark.asyncio
async def test_first_pass_runs_plan_executor_critic_and_final_save_in_order() -> None:
    service, context, planner, executor, critic, checkpoint, publisher, telemetry, timeline = _service(
        [CriticResult(passed=True)]
    )

    state = await service.run(_context(), media_id=7)

    assert context.select_calls
    assert len(planner.plan_calls) == 1
    assert len(executor.calls) == 1
    assert len(critic.calls) == 1
    assert state.critique is not None and state.critique.passed
    assert len(checkpoint.saved_results) == 1
    assert telemetry.counts == Counter({"criticRounds": 1, "criticPassed": 1})
    names = [item[0] for item in timeline]
    assert names.index("select") < names.index("plan") < names.index("execute") < names.index("critique")
    assert publisher.events[-1][1].stage is TaskStage.CRITIC_PASSED


@pytest.mark.asyncio
async def test_two_round_failure_refreshes_context_and_stops_on_second_pass() -> None:
    first = CriticResult(passed=False, requiredTimestamps=[30_000])
    service, context, _planner, executor, _critic, _checkpoint, publisher, telemetry, _timeline = _service(
        [first, CriticResult(passed=True)]
    )

    state = await service.run(_context(), media_id=7)

    assert len(executor.calls) == 2
    assert len(context.refine_calls) == 1
    assert telemetry.counts["criticEvidenceRefreshes"] == 1
    assert telemetry.counts["criticRewriteOnlyRetries"] == 0
    assert state.round == 2
    assert publisher.events[-1][1].stage is TaskStage.CRITIC_PASSED
    assert any(event.stage is TaskStage.EVIDENCE_REFRESHED for _, event in publisher.events)


@pytest.mark.asyncio
async def test_feedback_only_retry_reuses_selected_context_without_refresh_or_replan() -> None:
    first = CriticResult(passed=False, feedback=["rewrite wording"])
    service, context, planner, executor, _critic, _checkpoint, _publisher, telemetry, _timeline = _service(
        [first, CriticResult(passed=True)]
    )

    await service.run(_context(), media_id=7)

    assert len(executor.calls) == 2
    assert context.refine_calls == []
    assert telemetry.counts["criticRewriteOnlyRetries"] == 1
    assert telemetry.counts["criticEvidenceRefreshes"] == 0
    assert planner.replan_calls == []
    assert executor.calls[1][2] == first


@pytest.mark.asyncio
async def test_missing_requirement_replans_successfully_and_persists_new_plan() -> None:
    planner = PlannerFake([], replanned=_plan("new-task"))
    service, _context_fake, planner, executor, _critic, checkpoint, _publisher, telemetry, _timeline = _service(
        [CriticResult(passed=False, missingRequirements=["quiz"]), CriticResult(passed=True)],
        planner=planner,
    )

    state = await service.run(_context(), media_id=7)

    assert len(planner.replan_calls) == 1
    assert len(executor.calls) == 2
    assert executor.calls[1][1] == _plan("new-task")
    assert telemetry.counts["planRevisions"] == 1
    assert telemetry.counts["planRevisionFallbacks"] == 0
    assert any(saved_plan == _plan("new-task") for _, saved_plan in checkpoint.saved_plans)
    assert state.critique is not None and state.critique.passed


@pytest.mark.asyncio
async def test_replan_failure_is_swallowed_and_old_plan_is_used() -> None:
    planner = PlannerFake([], replan_error=RuntimeError("planner down"))
    service, _context_fake, planner, executor, _critic, _checkpoint, _publisher, telemetry, _timeline = _service(
        [CriticResult(passed=False, missingRequirements=["outline"]), CriticResult(passed=True)],
        planner=planner,
    )

    await service.run(_context(), media_id=7)

    assert len(planner.replan_calls) == 1
    assert telemetry.counts["planRevisionFallbacks"] == 1
    assert telemetry.counts["planRevisions"] == 0
    assert executor.calls[0][1] == executor.calls[1][1]


@pytest.mark.asyncio
async def test_draft_checkpoint_resumes_at_critic_without_executor() -> None:
    draft = AgentState(goal="goal", plan=_plan("cached"), result=_result(), round=1)
    service, _context_fake, planner, executor, critic, checkpoint, _publisher, telemetry, _timeline = _service(
        [CriticResult(passed=True)], state=draft
    )

    state = await service.run(_context(), media_id=7)

    assert planner.plan_calls == []
    assert executor.calls == []
    assert len(critic.calls) == 1
    assert state.round == 1
    assert telemetry.counts["criticCheckpointResumes"] == 1
    assert len(checkpoint.saved_results) == 1


@pytest.mark.asyncio
async def test_valid_terminal_checkpoint_returns_without_model_or_selection() -> None:
    terminal = AgentState(
        goal="goal",
        plan=_plan("cached"),
        result=_result(),
        critique=CriticResult(passed=True),
        round=1,
    )
    service, context, planner, executor, critic, checkpoint, publisher, telemetry, _timeline = _service(
        [CriticResult(passed=False)], state=terminal
    )

    returned = await service.run(_context(), media_id=7)

    assert returned == terminal
    assert context.select_calls == []
    assert planner.plan_calls == []
    assert executor.calls == []
    assert critic.calls == []
    assert publisher.events == []
    assert telemetry.counts["terminalCheckpointHits"] == 1
    assert len(checkpoint.saved_results) == 1


@pytest.mark.asyncio
async def test_invalid_terminal_checkpoint_resets_round_and_repairs_with_new_execution() -> None:
    invalid_terminal = AgentState(
        goal="goal",
        plan=_plan("cached"),
        result=AnalysisResult(title="", conclusions=[], evidence=[]),
        critique=CriticResult(passed=True),
        round=2,
    )
    service, context, planner, executor, critic, _checkpoint, _publisher, telemetry, _timeline = _service(
        [CriticResult(passed=True)], state=invalid_terminal
    )

    returned = await service.run(_context(), media_id=7)

    assert telemetry.counts["invalidTerminalCheckpointRepairs"] == 1
    assert context.select_calls
    assert planner.plan_calls == []  # the saved valid plan is reused
    assert len(executor.calls) == 1
    assert len(critic.calls) == 1
    assert returned.round == 1


@pytest.mark.asyncio
async def test_all_rounds_failed_keep_valid_result_and_publish_final_warning() -> None:
    service, _context_fake, _planner, executor, _critic, checkpoint, publisher, telemetry, _timeline = _service(
        [
            CriticResult(passed=False, feedback=["first"]),
            CriticResult(passed=False, feedback=["second"]),
        ]
    )

    state = await service.run(_context(), media_id=7)

    assert len(executor.calls) == 2
    assert state.round == 2
    assert state.result is not None
    assert state.critique is not None and not state.critique.passed
    assert telemetry.counts["criticRounds"] == 2
    assert publisher.events[-1][1].stage is TaskStage.ANALYSIS_COMPLETED_WITH_WARNINGS
    assert len(checkpoint.saved_results) == 1


@pytest.mark.asyncio
async def test_mode_isolation_uses_profile_mode_in_checkpoint_and_prompt_keys() -> None:
    checkpoint = CheckpointFake([])
    # Use one shared checkpoint for two independent services so a mode cannot
    # accidentally reuse the other mode's terminal state.
    learning = _service([CriticResult(passed=True)])[0]
    review = _service([CriticResult(passed=True)])[0]
    learning._checkpoint = checkpoint
    review._checkpoint = checkpoint

    await learning.run(
        _context(), media_id=7, profile=ModeProfile(mode=AnalysisMode.LEARNING)
    )
    await review.run(
        _context(), media_id=7, profile=ModeProfile(mode=AnalysisMode.REVIEW)
    )

    assert [key.mode for key in checkpoint.load_state_calls] == [
        AnalysisMode.LEARNING,
        AnalysisMode.REVIEW,
    ]
    assert {key.mode for key, _ in checkpoint.saved_results} == {
        AnalysisMode.LEARNING,
        AnalysisMode.REVIEW,
    }


def test_runtime_rejects_zero_round_budget_while_schema_allows_it() -> None:
    with pytest.raises(InvalidBudgetError, match="至少需要一轮"):
        _service([CriticResult(passed=True)], max_rounds=0)

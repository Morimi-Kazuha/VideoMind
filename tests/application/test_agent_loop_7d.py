from __future__ import annotations

from collections import Counter

import pytest

from dovideo.application import AgentLoopService
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisMode,
    AnalysisEvidence,
    AnalysisResult,
    AnalysisSection,
    CriticResult,
    ModeProfile,
    TaskStage,
    VideoContext,
    VideoSegment,
)


def _context() -> VideoContext:
    return VideoContext(
        source="video.mp4",
        user_goal="goal",
        segments=[
            VideoSegment(startMs=0, endMs=60_000, transcript="the claim"),
        ],
    )


def _plan() -> AgentPlan:
    return AgentPlan(understoodGoal="goal", tasks=["task"])


def _valid_result() -> AnalysisResult:
    return AnalysisResult(
        title="title",
        conclusions=["claim"],
        evidence=[
            AnalysisEvidence(
                timestampMs=1_000,
                source="ASR",
                content="the claim",
                claim="claim",
            )
        ],
    )


class ContextFake:
    async def select_relevant(self, context: VideoContext, media_id: int | None = None) -> VideoContext:
        return context


class PlannerFake:
    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        return _plan()

    async def repair_plan(self, context: VideoContext, invalid_plan: AgentPlan, *, instruction: str = "") -> AgentPlan:
        return _plan()


class ExecutorFake:
    async def execute(self, context: VideoContext, plan: AgentPlan, previous_critique: CriticResult | None = None, *, instruction: str = "") -> AnalysisResult:
        return _valid_result()


class CriticFake:
    def __init__(self, value: CriticResult | None) -> None:
        self.value = value
        self.calls: list[tuple[object, ...]] = []

    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult | None,
        *,
        instruction: str = "",
    ) -> CriticResult | None:
        self.calls.append((context, plan, result, instruction))
        return self.value


class CheckpointFake:
    def __init__(self, calls: list[tuple[object, ...]]) -> None:
        self.calls = calls

    async def save_critic_state(self, key: object, state: AgentState) -> None:
        self.calls.append(("save_critic", key, state))


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


def _service(
    critic_value: CriticResult | None,
    *,
    max_rounds: int = 2,
) -> tuple[AgentLoopService, CriticFake, CheckpointFake, PublisherFake, TelemetryFake, list[tuple[object, ...]]]:
    calls: list[tuple[object, ...]] = []
    critic = CriticFake(critic_value)
    checkpoint = CheckpointFake(calls)
    publisher = PublisherFake(calls)
    telemetry = TelemetryFake()
    service = AgentLoopService(
        ContextFake(), PlannerFake(), ExecutorFake(), checkpoint, publisher, telemetry, critic,
        budget_config={"maxRounds": max_rounds},
    )
    return service, critic, checkpoint, publisher, telemetry, calls


@pytest.mark.asyncio
async def test_null_critic_is_normalized_and_saved_after_structure_and_evidence_guards() -> None:
    service, critic, _checkpoint, _publisher, telemetry, calls = _service(None)

    state = await service.critique_round(_context(), _plan(), _valid_result(), media_id=7, round=1)

    assert critic.calls[0][3] == ""
    assert state.critique is not None
    assert not state.critique.passed
    assert state.critique.feedback == ("Critic 未返回有效结果",)
    assert telemetry.counts == Counter({"criticRounds": 1})
    assert [call[0] for call in calls] == ["event", "save_critic", "event"]
    assert calls[0][2].stage is TaskStage.CRITIC_STARTED
    assert calls[2][2].stage is TaskStage.CRITIC_RETRY_REQUIRED


@pytest.mark.asyncio
async def test_passed_critic_with_declared_feedback_is_forced_failed() -> None:
    service, _critic, _checkpoint, _publisher, telemetry, calls = _service(
        CriticResult(passed=True, feedback=["problem"])
    )

    state = await service.critique_round(_context(), _plan(), _valid_result(), media_id=1)

    assert state.critique is not None and not state.critique.passed
    assert state.critique.feedback == ("problem",)
    assert telemetry.counts["criticRounds"] == 1
    assert telemetry.counts["criticPassed"] == 0
    assert calls[-1][2].stage is TaskStage.CRITIC_RETRY_REQUIRED


@pytest.mark.asyncio
async def test_failed_critic_without_details_gets_default_feedback() -> None:
    service, _critic, _checkpoint, _publisher, _telemetry, calls = _service(
        CriticResult(passed=False)
    )

    state = await service.critique_round(_context(), _plan(), _valid_result(), media_id=1)

    assert state.critique is not None
    assert state.critique.feedback == ("重新检查目标覆盖、结构完整性和证据绑定",)
    assert calls[-1][2].stage is TaskStage.CRITIC_RETRY_REQUIRED
    assert not service.requires_evidence_refresh(state.critique)


@pytest.mark.asyncio
async def test_structure_and_mode_section_missing_are_retry_only() -> None:
    profile = ModeProfile(mode=AnalysisMode.LEARNING, requiredSectionKeys=["outline"])
    incomplete = AnalysisResult(title="", conclusions=[], evidence=[], sections=[])
    service, _critic, _checkpoint, _publisher, _telemetry, calls = _service(
        CriticResult(passed=True)
    )

    state = await service.critique_round(
        _context(), _plan(), incomplete, media_id=1, profile=profile
    )

    assert state.critique is not None and not state.critique.passed
    assert "补充明确的产物标题" in state.critique.feedback
    assert "补充当前分析模式要求的结构化段落: outline" in state.critique.feedback
    assert not service.requires_evidence_refresh(state.critique)
    assert calls[-1][2].stage is TaskStage.CRITIC_RETRY_REQUIRED


@pytest.mark.asyncio
async def test_invalid_evidence_and_unsupported_claim_request_refresh_and_count_passes_only_after_guards() -> None:
    bad_result = AnalysisResult(
        title="title",
        conclusions=["unsupported conclusion"],
        evidence=[
            AnalysisEvidence(
                timestampMs=120_000,
                source="ASR",
                content="fabricated",
                claim="unsupported conclusion",
            )
        ],
    )
    service, _critic, _checkpoint, _publisher, telemetry, calls = _service(
        CriticResult(passed=True)
    )

    state = await service.critique_round(_context(), _plan(), bad_result, media_id=1)

    assert state.critique is not None and not state.critique.passed
    assert state.critique.required_timestamps == (120_000,)
    assert state.critique.unsupported_claims
    assert service.requires_evidence_refresh(state.critique)
    assert telemetry.counts == Counter({"criticRounds": 1})
    assert calls[-1][2].stage is TaskStage.CRITIC_RETRY_REQUIRED


@pytest.mark.asyncio
async def test_passed_result_saves_critic_state_and_publishes_passed_stage() -> None:
    service, _critic, _checkpoint, _publisher, telemetry, calls = _service(
        CriticResult(passed=True)
    )

    state = await service.critique_round(_context(), _plan(), _valid_result(), media_id=3, round=1)

    assert state.critique is not None and state.critique.passed
    assert telemetry.counts == Counter({"criticRounds": 1, "criticPassed": 1})
    assert calls[-1][2].stage is TaskStage.CRITIC_PASSED
    assert calls[-1][2].message == "Critic 校验通过，正在整理结构化结果"
    assert calls[-2][0] == "save_critic"


@pytest.mark.asyncio
async def test_failed_final_round_publishes_warning_stage() -> None:
    service, _critic, _checkpoint, _publisher, _telemetry, calls = _service(
        CriticResult(passed=False, feedback=["rewrite"]), max_rounds=2
    )

    state = await service.critique_round(_context(), _plan(), _valid_result(), media_id=3, round=2)

    assert state.critique is not None and not state.critique.passed
    assert calls[-1][2].stage is TaskStage.ANALYSIS_COMPLETED_WITH_WARNINGS
    assert calls[-1][2].message == "Critic 达到最大校验轮次，正在保留警告并生成结果"

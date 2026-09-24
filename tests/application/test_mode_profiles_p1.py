from __future__ import annotations

import pytest

from dovideo.application import (
    InvalidResultError,
    is_result_valid,
    missing_section_keys,
    mode_profile_for,
    TaskKey,
)
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisMode,
    AnalysisResult,
    AnalysisSection,
    CriticResult,
)
from tests.application.test_agent_loop_7e import CheckpointFake, _context
from tests.application.test_agent_loop_7f import _build


_EXPECTED_KEYS = {
    AnalysisMode.GENERAL: (),
    AnalysisMode.LEARNING: (
        "knowledge_outline",
        "difficult_points",
        "review_questions",
    ),
    AnalysisMode.REVIEW: (
        "strengths",
        "issues_and_risks",
        "omissions",
        "improvements",
    ),
    AnalysisMode.CREATION: (
        "key_moments",
        "hooks_and_titles",
        "script_outline",
        "adaptation_ideas",
    ),
}
_CONCRETE_MODES = (
    AnalysisMode.LEARNING,
    AnalysisMode.REVIEW,
    AnalysisMode.CREATION,
)


def _mode_result(
    profile,
    *,
    omitted_key: str | None = None,
    blank_key: str | None = None,
    evidence_content: str = "the claim",
) -> AnalysisResult:
    sections = tuple(
        AnalysisSection(
            key=key,
            title=key.replace("_", " ").title(),
            items=("specific source-grounded artifact",)
            if key != blank_key
            else ("  ",),
        )
        for key in profile.required_section_keys
        if key != omitted_key
    )
    return AnalysisResult(
        title="Mode analysis",
        conclusions=("claim",),
        evidence=(
            AnalysisEvidence(
                timestamp_ms=1_000,
                source="ASR",
                content=evidence_content,
                claim="claim",
            ),
        ),
        suggestions=("Use the verified source context",),
        sections=sections,
    )


class _ModeKeyedCheckpoint(CheckpointFake):
    """Return a saved draft only under its original concrete task identity."""

    def __init__(self, expected_key: TaskKey, state: AgentState) -> None:
        super().__init__([], state=None)
        self.expected_key = expected_key
        self.saved_state = state

    async def load_critic_state(self, key: TaskKey) -> AgentState | None:
        self.timeline.append(("load_state", key))
        self.load_state_calls.append(key)
        return self.saved_state if key == self.expected_key else None


@pytest.mark.parametrize("mode", tuple(AnalysisMode))
def test_registry_resolves_one_deterministic_profile_for_every_mode(mode) -> None:
    first = mode_profile_for(mode)
    second = mode_profile_for(mode)

    assert first is second
    assert first.mode is mode
    assert first.required_section_keys == _EXPECTED_KEYS[mode]
    if mode is AnalysisMode.GENERAL:
        assert first.plan_instruction == ""
        assert first.execute_instruction == ""
        assert first.critic_instruction == ""
        assert is_result_valid(_mode_result(first), first)
    else:
        assert first.display_name
        assert first.plan_instruction.strip()
        assert first.execute_instruction.strip()
        assert first.critic_instruction.strip()
        assert first.required_section_keys


def test_registry_rejects_non_enum_mode_values() -> None:
    with pytest.raises(TypeError, match="AnalysisMode"):
        mode_profile_for("LEARNING")  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", _CONCRETE_MODES)
@pytest.mark.asyncio
async def test_agent_loop_passes_mode_contract_to_all_three_roles(mode) -> None:
    profile = mode_profile_for(mode)
    service, _context_service, planner, executor, critic, _checkpoint, _telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[CriticResult(passed=True)],
        results=[_mode_result(profile)],
    )

    state = await service.run(_context(), media_id=7, profile=profile)

    assert state.critique is not None and state.critique.passed
    assert planner.plan_calls[0][1] == profile.plan_instruction
    assert executor.calls[0][3] == profile.execute_instruction
    assert critic.calls[0][3] == profile.critic_instruction


@pytest.mark.parametrize("mode", _CONCRETE_MODES)
@pytest.mark.asyncio
async def test_missing_required_mode_artifact_fails_agent_semantics(mode) -> None:
    profile = mode_profile_for(mode)
    missing_key = profile.required_section_keys[0]
    service, _context_service, _planner, executor, _critic, checkpoint, _telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[CriticResult(passed=True)],
        results=[_mode_result(profile, omitted_key=missing_key)],
    )

    with pytest.raises(InvalidResultError):
        await service.run(_context(), media_id=7, profile=profile)

    assert len(executor.calls) == 1
    assert len(checkpoint.saved_critics) == 1
    saved_critique = checkpoint.saved_critics[0][1].critique
    assert saved_critique is not None and not saved_critique.passed
    assert any(missing_key in feedback for feedback in saved_critique.feedback)


def test_required_artifact_with_only_blank_items_is_not_present() -> None:
    profile = mode_profile_for(AnalysisMode.LEARNING)
    blank_key = profile.required_section_keys[0]
    result = _mode_result(profile, blank_key=blank_key)

    assert missing_section_keys(result, profile) == (blank_key,)
    assert not is_result_valid(result, profile)


@pytest.mark.parametrize("mode", _CONCRETE_MODES)
@pytest.mark.asyncio
async def test_mode_sections_do_not_bypass_evidence_guard(mode) -> None:
    profile = mode_profile_for(mode)
    service, _context_service, _planner, _executor, _critic, _checkpoint, _telemetry = _build(
        budget={"maxRounds": 1},
        critiques=[CriticResult(passed=True)],
        results=[_mode_result(profile, evidence_content="not in the source")],
    )

    state = await service.run(_context(), media_id=7, profile=profile)

    assert state.critique is not None and not state.critique.passed
    assert "claim" in state.critique.unsupported_claims
    assert state.critique.required_timestamps == (1_000,)


@pytest.mark.parametrize("mode", _CONCRETE_MODES)
@pytest.mark.asyncio
async def test_checkpoint_draft_resumes_under_the_same_mode_identity(mode) -> None:
    profile = mode_profile_for(mode)
    task_key = TaskKey(7, "goal", mode)
    draft = AgentState(
        goal="goal",
        plan=AgentPlan(understood_goal="goal", tasks=("review source",)),
        result=_mode_result(profile),
        round=1,
    )
    keyed_checkpoint = _ModeKeyedCheckpoint(task_key, draft)
    service, _context_service, planner, executor, critic, _checkpoint, _telemetry = _build(
        budget={"maxRounds": 2},
        critiques=[CriticResult(passed=True)],
    )
    service._checkpoint = keyed_checkpoint

    state = await service.run(_context(), media_id=7, profile=profile)

    assert keyed_checkpoint.load_state_calls == [task_key]
    assert keyed_checkpoint.saved_critics[0][0] == task_key
    assert keyed_checkpoint.saved_results[0][0] == task_key
    assert planner.plan_calls == []
    assert executor.calls == []
    assert critic.calls[0][3] == profile.critic_instruction
    assert state.critique is not None and state.critique.passed

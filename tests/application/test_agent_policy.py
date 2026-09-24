from __future__ import annotations

import pytest

from dovideo.application import (
    AgentBudgetConfig,
    AgentValidationPolicy,
    BudgetUsage,
    InvalidPlanError,
    InvalidResultError,
    enforce_structure_bounds,
    is_plan_valid,
    is_result_valid,
    java_string_length,
    missing_section_keys,
    validate_budget_config,
    validate_budget_usage,
    validate_plan,
    validate_result,
)
from dovideo.domain import (
    AgentPlan,
    AnalysisEvidence,
    AnalysisResult,
    AnalysisSection,
    CriticResult,
    ModeProfile,
)


def test_plan_uses_java_utf16_units_at_500_boundary() -> None:
    astral = "😀" * 250
    assert java_string_length(astral) == 500
    assert is_plan_valid(AgentPlan(understoodGoal="goal", tasks=[astral]))
    assert not is_plan_valid(AgentPlan(understoodGoal="goal", tasks=[astral + "😀"]))


def test_plan_count_and_fail_fast_error_match_agent_loop_guard() -> None:
    invalid = AgentPlan(understoodGoal="goal", tasks=[str(i) for i in range(6)])
    assert not AgentValidationPolicy.isPlanValid(invalid)
    with pytest.raises(InvalidPlanError, match="Planner"):
        validate_plan(invalid)


def _result(*, sections: list[AnalysisSection] | None = None) -> AnalysisResult:
    return AnalysisResult(
        title="title",
        conclusions=["claim"],
        evidence=[AnalysisEvidence(timestampMs=1, source="ASR", content="claim")],
        sections=sections or [],
    )


def test_result_requires_common_fields_and_profile_sections() -> None:
    profile = ModeProfile(requiredSectionKeys=["outline", "quiz"])
    complete = _result(
        sections=[
            AnalysisSection(key="outline", title="Outline", items=["item"]),
            AnalysisSection(key=" quiz ", title="Quiz", items=["item"]),
        ]
    )
    assert is_result_valid(complete, profile)
    assert missing_section_keys(complete, profile) == ()
    assert not is_result_valid(_result(), profile)
    with pytest.raises(InvalidResultError, match="Executor"):
        validate_result(_result(), profile)


def test_structure_guard_preserves_critic_fields_and_targets_missing_work() -> None:
    profile = ModeProfile(requiredSectionKeys=["outline"])
    critique = CriticResult(
        passed=True,
        feedback=["semantic feedback"],
        missingRequirements=["task"],
        unsupportedClaims=["claim"],
        requiredTimestamps=[10],
    )
    guarded = enforce_structure_bounds(AnalysisResult(title=""), critique, profile)
    assert not guarded.passed
    assert guarded.feedback[:1] == ("semantic feedback",)
    assert guarded.missing_requirements == ("task",)
    assert guarded.unsupported_claims == ("claim",)
    assert guarded.required_timestamps == (10,)
    assert "补充明确的产物标题" in guarded.feedback
    assert "补充当前分析模式要求的结构化段落: outline" in guarded.feedback


def test_budget_policy_revalidates_mapping_boundaries() -> None:
    config = validate_budget_config({"maxRounds": 2, "maxEstimatedCost": 0.5})
    usage = validate_budget_usage({"estimatedTokens": 3, "estimatedCost": 0.1})
    assert isinstance(config, AgentBudgetConfig)
    assert isinstance(usage, BudgetUsage)

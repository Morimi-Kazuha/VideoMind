from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from dovideo.domain import AgentPlan, AgentState, AnalysisResult, CriticResult


def test_agent_plan_normalizes_goal_and_exposes_service_bounds() -> None:
    plan = AgentPlan(understoodGoal="  explain  ", tasks=["one"])
    assert plan.understood_goal == "explain"
    assert plan.is_execution_valid()
    assert plan.execution_violations == ()
    nested = AgentState.AgentPlan(understoodGoal="  nested  ", tasks=["nested-task"])
    assert nested.understood_goal == "nested"
    assert nested.tasks == ("nested-task",)


def test_agent_plan_parser_preserves_java_permissiveness_for_repair() -> None:
    invalid = AgentPlan(understoodGoal="", tasks=["", *[str(i) for i in range(6)]])
    assert not invalid.is_execution_valid()
    assert "understoodGoal is required" in invalid.execution_violations
    assert "at most five tasks are allowed" in invalid.execution_violations
    # DTO parsing is separate from AgentLoop execution validation.
    assert len(invalid.tasks) == 7


def test_agent_state_requires_nonblank_goal_and_nonnegative_round() -> None:
    state = AgentState(goal="  goal ", round=0)
    assert state.goal == "goal"
    with pytest.raises(ValidationError):
        AgentState(goal=" ")
    with pytest.raises(ValidationError):
        AgentState(goal="goal", round=-1)


def test_critic_defaults_and_alias_round_trip() -> None:
    critique = CriticResult(
        passed=True,
        feedback=["ok"],
        missingRequirements=["m"],
        unsupportedClaims=["u"],
        requiredTimestamps=[100],
    )
    payload = json.loads(critique.model_dump_json(by_alias=True))
    assert payload["missingRequirements"] == ["m"]
    assert CriticResult.model_validate(payload) == critique
    assert CriticResult(passed=None).passed is False


def test_nested_state_round_trip_and_defensive_copy() -> None:
    tasks = ["one"]
    plan = AgentPlan(understoodGoal="goal", tasks=tasks)
    state = AgentState(
        goal="goal",
        plan=plan,
        result=AnalysisResult(title="result"),
        critique=AgentState.CriticResult(passed=False),
        round=1,
    )
    tasks.append("two")
    assert state.plan.tasks == ("one",)
    restored = AgentState.model_validate(json.loads(state.model_dump_json(by_alias=True)))
    assert restored == state

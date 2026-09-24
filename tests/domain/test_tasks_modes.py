from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from dovideo.domain import (
    AnalysisMode,
    ModeProfile,
    TaskEvent,
    TaskStage,
    TaskStatus,
    TaskStatusState,
)


def test_analysis_mode_has_java_fallback_and_strict_request_parsers() -> None:
    assert AnalysisMode.from_nullable(None) is AnalysisMode.GENERAL
    assert AnalysisMode.from_nullable(" learning ") is AnalysisMode.LEARNING
    assert AnalysisMode.from_nullable("not-a-mode") is AnalysisMode.GENERAL
    assert AnalysisMode.from_request("review") is AnalysisMode.REVIEW
    with pytest.raises(ValueError, match="不支持的分析模式"):
        AnalysisMode.from_request("not-a-mode")


def test_task_stage_unknown_values_return_none_without_trimming() -> None:
    assert TaskStage.from_value(None) is None
    assert TaskStage.from_value(" ") is None
    assert TaskStage.from_value("COMPLETED") is TaskStage.COMPLETED
    assert TaskStage.from_value(" COMPLETED ") is None


def test_task_status_factories_and_event_terminal_semantics() -> None:
    status = TaskStatus.of(TaskStatusState.PROCESSING, "working")
    event = TaskEvent.of(status, TaskStage.AGENT_LOOP)
    assert event.state is TaskStatusState.PROCESSING
    assert not event.terminal()
    completed = TaskStatus.completed("markdown")
    terminal = TaskEvent.of(completed, TaskStage.COMPLETED)
    assert completed.message == "任务完成"
    assert terminal.terminal()
    assert TaskStatus.State.COMPLETED is TaskStatusState.COMPLETED


def test_task_status_agent_warning_is_preserved() -> None:
    from dovideo.domain import AgentState, AnalysisResult, CriticResult

    state = AgentState(
        goal="goal",
        result=AnalysisResult(title="t", conclusions=["c"]),
        critique=CriticResult(passed=False),
    )
    status = TaskStatus.completed(state)
    assert status.state is TaskStatusState.COMPLETED
    assert status.result.startswith("> **结果提示：**")
    assert "人工核验" in status.message


def test_task_event_wire_round_trip() -> None:
    event = TaskEvent(
        state=TaskStatusState.FAILED,
        result=None,
        message="failed",
        stage=TaskStage.DEAD_LETTERED,
    )
    payload = json.loads(event.model_dump_json(by_alias=True))
    assert TaskEvent.model_validate(payload) == event


def test_mode_profile_deduplicates_required_keys() -> None:
    profile = ModeProfile(
        mode=AnalysisMode.LEARNING,
        displayName="学习",
        requiredSectionKeys=["outline", "outline", "quiz"],
    )
    assert profile.required_section_keys == ("outline", "quiz")
    assert profile.plan_instruction == ""


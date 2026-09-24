from __future__ import annotations

import json

import pytest

from dovideo.application import InvalidResultError, mode_profile_for, validate_result
from dovideo.domain import AnalysisMode, AgentPlan, VideoContext, VideoSegment
from dovideo.infrastructure.providers import (
    CriticModelAdapter,
    ExecutorModelAdapter,
    PlannerModelAdapter,
)


class _CapturingChat:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def complete(self, messages, *, stage: str):
        self.calls.append({"messages": messages, "stage": stage})
        return self.responses.pop(0)


def _context() -> VideoContext:
    return VideoContext(
        source="memory://video",
        user_goal="explain the source",
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=2_000,
                transcript="the source claim",
            ),
        ),
    )


def _responses() -> list[str]:
    return [
        json.dumps({"understoodGoal": "explain", "tasks": ["inspect source"]}),
        json.dumps(
            {
                "title": "Analysis",
                "conclusions": ["claim"],
                "evidence": [
                    {
                        "timestampMs": 100,
                        "source": "ASR",
                        "content": "the source claim",
                        "claim": "claim",
                    }
                ],
                "suggestions": [],
                "sections": [],
            }
        ),
        json.dumps(
            {
                "passed": True,
                "feedback": [],
                "missingRequirements": [],
                "unsupportedClaims": [],
                "requiredTimestamps": [],
            }
        ),
    ]


def _user_prompt(call: dict[str, object]) -> str:
    messages = call["messages"]
    assert isinstance(messages, tuple)
    return str(messages[1]["content"])


@pytest.mark.parametrize("mode", tuple(AnalysisMode))
@pytest.mark.asyncio
async def test_resolved_profile_instruction_reaches_each_model_adapter(mode) -> None:
    profile = mode_profile_for(mode)
    chat = _CapturingChat(_responses())
    context = _context()

    await PlannerModelAdapter(chat).plan(
        context,
        instruction=profile.plan_instruction,
    )
    result = await ExecutorModelAdapter(chat).execute(
        context,
        AgentPlan(understood_goal="explain", tasks=("inspect source",)),
        instruction=profile.execute_instruction,
    )
    await CriticModelAdapter(chat).critique(
        context,
        AgentPlan(understood_goal="explain", tasks=("inspect source",)),
        result,
        instruction=profile.critic_instruction,
    )

    assert [call["stage"] for call in chat.calls] == ["PLANNER", "EXECUTOR", "CRITIC"]
    planner_prompt, executor_prompt, critic_prompt = map(_user_prompt, chat.calls)
    if mode is AnalysisMode.GENERAL:
        assert "Additional mode planning requirements:" not in planner_prompt
        assert "Additional mode output requirements:" not in executor_prompt
        assert "Additional mode review requirements:" not in critic_prompt
    else:
        assert profile.plan_instruction in planner_prompt
        assert profile.execute_instruction in executor_prompt
        assert profile.critic_instruction in critic_prompt
        for key in profile.required_section_keys:
            assert key in executor_prompt
            assert key in critic_prompt


@pytest.mark.asyncio
async def test_missing_mode_section_is_semantic_not_structural_model_failure() -> None:
    profile = mode_profile_for(AnalysisMode.LEARNING)
    chat = _CapturingChat(
        [
            json.dumps(
                {
                    "title": "Learning result",
                    "conclusions": ["claim"],
                    "evidence": [
                        {
                            "timestampMs": 100,
                            "source": "ASR",
                            "content": "the source claim",
                            "claim": "claim",
                        }
                    ],
                    "suggestions": [],
                    "sections": [],
                }
            )
        ]
    )

    result = await ExecutorModelAdapter(chat).execute(
        _context(),
        AgentPlan(understood_goal="explain", tasks=("inspect source",)),
        instruction=profile.execute_instruction,
    )

    assert len(chat.calls) == 1
    assert "Additional mode output requirements:" in _user_prompt(chat.calls[0])
    with pytest.raises(InvalidResultError):
        validate_result(result, profile)

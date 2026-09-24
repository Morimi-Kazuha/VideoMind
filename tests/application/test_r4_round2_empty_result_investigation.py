from __future__ import annotations

import json

import pytest

from dovideo.application import AgentLoopService, InvalidResultError
from dovideo.domain import (
    AgentPlan,
    AnalysisResult,
    CriticResult,
    VideoContext,
    VideoSegment,
)
from dovideo.infrastructure.providers import ExecutorModelAdapter


class _ChatStub:
    """Offline structured-chat stub; no network or provider configuration."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def complete(self, messages, *, stage: str):
        self.calls.append({"messages": messages, "stage": stage})
        return self.responses.pop(0)


class _ContextStub:
    async def select_relevant(self, context: VideoContext, media_id=None) -> VideoContext:
        return context


class _PlannerStub:
    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        return AgentPlan(understoodGoal=context.user_goal, tasks=("bind evidence",))

    async def repair_plan(self, context, invalid_plan, *, instruction: str = "") -> AgentPlan:
        return AgentPlan(understoodGoal=context.user_goal, tasks=("bind evidence",))

    async def replan(self, context, current_plan, critique, *, instruction: str = "") -> AgentPlan:
        return current_plan


class _CriticStub:
    def __init__(self) -> None:
        self.calls: list[tuple[VideoContext, AgentPlan, AnalysisResult]] = []

    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult,
        *,
        instruction: str = "",
    ) -> CriticResult:
        self.calls.append((context, plan, result))
        return CriticResult(passed=False, feedback=("rewrite the result",))


class _CheckpointStub:
    async def load_critic_state(self, key):
        return None

    async def load_plan(self, key):
        return None

    async def save_plan(self, key, plan):
        return None

    async def save_execution_state(self, key, state):
        return None

    async def save_critic_state(self, key, state):
        return None

    async def save_result(self, key, state):
        return None


class _EventsStub:
    async def publish(self, key, event):
        return None


class _TelemetryStub:
    def increment(self, metric: str, amount: int = 1, **kwargs) -> None:
        return None


def _context() -> VideoContext:
    return VideoContext(
        source="memory://r4-investigation",
        user_goal="find timestamped evidence",
        segments=(
            VideoSegment(
                start_ms=0,
                end_ms=60_000,
                transcript="opening evidence",
            ),
            VideoSegment(
                start_ms=60_000,
                end_ms=120_000,
                transcript="later evidence",
            ),
        ),
    )


def _valid_result_json() -> str:
    return json.dumps(
        {
            "title": "first draft",
            "conclusions": ["supported conclusion"],
            "evidence": [
                {
                    "timestampMs": 0,
                    "source": "ASR",
                    "content": "opening evidence",
                    "claim": "supported conclusion",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_r4_round2_empty_result_reproduces_existing_validation_boundary() -> None:
    """Reproduce the observed post-adapter empty result without a live call.

    The raw provider response was not retained by the production trace.  The
    second stub response is therefore the minimal JSON object consistent with
    the persisted post-adapter shape: all AnalysisResult fields are present,
    but conclusions and evidence are empty.
    """

    chat = _ChatStub(
        [
            _valid_result_json(),
            json.dumps(
                {
                    "title": "未命名分析",
                    "conclusions": [],
                    "evidence": [],
                    "suggestions": [],
                    "sections": [],
                }
            ),
        ]
    )
    critic = _CriticStub()
    executor = ExecutorModelAdapter(chat)
    service = AgentLoopService(
        _ContextStub(),
        _PlannerStub(),
        executor,
        _CheckpointStub(),
        _EventsStub(),
        _TelemetryStub(),
        critic,
        budget_config={"maxRounds": 2},
    )

    with pytest.raises(InvalidResultError, match="Executor"):
        await service.run(_context(), media_id=14)

    executor_calls = [call for call in chat.calls if call["stage"] == "EXECUTOR"]
    assert len(executor_calls) == 2
    second_prompt = next(
        message["content"]
        for message in executor_calls[1]["messages"]
        if message["role"] == "user"
    )
    assert "Plan:" in second_prompt
    assert "PreviousCritique:" in second_prompt
    assert "VideoContext:" in second_prompt
    assert "PreviousResult:" not in second_prompt
    assert "PreviousDraft:" not in second_prompt

    assert len(critic.calls) == 2
    assert critic.calls[1][2] == AnalysisResult()
    assert critic.calls[0][0] == critic.calls[1][0]
    assert critic.calls[0][1] == critic.calls[1][1]

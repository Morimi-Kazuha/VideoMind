from __future__ import annotations

import json

import pytest

from dovideo.application import VideoContextBuilder
from dovideo.application.value_objects import (
    AsrBranchOutcome,
    MediaObservationBundle,
    OcrBranchOutcome,
    TranscriptSpan,
)
from dovideo.infrastructure.persistence import JsonCheckpointCodec
from dovideo.infrastructure.providers import PlannerModelAdapter


class _CapturingChat:
    def __init__(self) -> None:
        self.prompt = ""

    async def complete(self, messages, *, stage):
        assert stage == "PLANNER"
        self.prompt = messages[1]["content"]
        return '{"understoodGoal":"find speech","tasks":["locate statement"]}'


@pytest.mark.asyncio
async def test_original_observations_persist_but_do_not_duplicate_agent_prompt() -> None:
    context = VideoContextBuilder().build(
        "memory://video",
        "find speech",
        MediaObservationBundle(
            asr=AsrBranchOutcome(
                observations=(TranscriptSpan(3000, 5000, "precise source speech"),),
                attempted=1,
            ),
            ocr=OcrBranchOutcome(),
        ),
    )
    assert len(context.observations) == 1
    stored = json.loads(JsonCheckpointCodec().encode(context))["payload"]
    assert stored["observations"][0]["text"] == "precise source speech"

    chat = _CapturingChat()
    await PlannerModelAdapter(chat).plan(context)
    prompt_context = json.loads(chat.prompt.split("VideoContext:\n", 1)[1])
    assert "observations" not in prompt_context
    assert prompt_context["segments"][0]["transcript"] == "precise source speech"

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
from dovideo.infrastructure.providers import ChunkSummaryModelAdapter, PlannerModelAdapter
from dovideo.infrastructure.providers.model import _dump_json


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
    assert "sourceItems" not in prompt_context["segments"][0]
    assert "sourceRevision" not in prompt_context["segments"][0]
    assert "evidenceFrames" not in prompt_context["segments"][0]
    assert prompt_context["segments"][0]["startMs"] == 0


@pytest.mark.asyncio
async def test_chunk_summary_receives_exact_text_and_time_without_identity_duplication() -> None:
    context = VideoContextBuilder().build(
        "memory://video",
        "summarize",
        MediaObservationBundle(
            asr=AsrBranchOutcome(
                observations=(TranscriptSpan(3000, 5000, "precise source speech"),),
                attempted=1,
            ),
            ocr=OcrBranchOutcome(),
        ),
    )

    class SummaryChat:
        prompt = ""

        async def complete(self, messages, *, stage):
            assert stage == "CHUNK_SUMMARY"
            self.prompt = messages[1]["content"]
            return '{"segmentSummary":"precise source speech","keywords":["speech"]}'

    chat = SummaryChat()
    await ChunkSummaryModelAdapter(chat).summarize_chunk(context.segments)
    segments = json.loads(chat.prompt.split("RawSegments:\n", 1)[1])
    assert segments == [
        {
            "startMs": 0,
            "endMs": 60000,
            "transcript": "precise source speech",
            "ocrTexts": [],
        }
    ]


def test_evidence_prompt_keeps_exact_source_lines_without_checkpoint_metadata() -> None:
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

    prompt = json.loads(_dump_json(context, evidence_items=True))
    segment = prompt["segments"][0]
    assert segment["sourceItems"] == [
        {
            "source": "ASR",
            "startMs": 3000,
            "endMs": 5000,
            "text": "precise source speech",
        }
    ]
    assert "transcript" not in segment
    assert "sourceRevision" not in segment
    assert "sourceItemId" not in json.dumps(segment)

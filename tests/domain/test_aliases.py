"""Regression tests for Python/Jackson dual-name input handling."""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from dovideo.domain import (
    AgentPlan,
    AnalysisEvidence,
    AnalysisMode,
    ChunkSummary,
    CriticResult,
    ModeProfile,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
    VideoRetrievalIntent,
    VideoSegment,
)


def test_context_goal_snake_camel_none_and_omitted() -> None:
    assert VideoContext(source="v", user_goal="snake").user_goal == "snake"
    assert VideoContext(source="v", userGoal="camel").user_goal == "camel"
    assert VideoContext(source="v").user_goal == ""
    assert VideoContext(source="v", user_goal=None).user_goal == ""
    assert VideoContext(source="v", userGoal=None).user_goal == ""


def test_segment_aliases_and_equal_dual_values() -> None:
    snake = VideoSegment(start_ms=0, end_ms=1, ocr_texts=["ocr"], evidence_frames=["frame"])
    camel = VideoSegment(startMs=0, endMs=1, ocrTexts=["ocr"], evidenceFrames=["frame"])
    both = VideoSegment(
        start_ms=0,
        startMs=0,
        end_ms=1,
        endMs=1,
        ocr_texts=["ocr"],
        ocrTexts=["ocr"],
        evidence_frames=["frame"],
        evidenceFrames=["frame"],
    )
    assert snake == camel == both
    assert VideoSegment(start_ms=0, end_ms=1, ocr_texts=None).ocr_texts == ()
    assert VideoSegment(start_ms=0, end_ms=1, ocrTexts=None).ocr_texts == ()
    assert VideoSegment(start_ms=0, end_ms=1).evidence_frames == ()


def test_chunk_summary_and_chunk_aliases_cover_none_and_omitted() -> None:
    assert ChunkSummary(segment_summary="snake").segment_summary == "snake"
    assert ChunkSummary(segmentSummary="camel").segment_summary == "camel"
    assert ChunkSummary(segment_summary=None).segment_summary == ""
    assert ChunkSummary(segmentSummary=None).segment_summary == ""
    assert ChunkSummary().segment_summary == ""
    assert ChunkSummary(segment_summary="x", segmentSummary="x").segment_summary == "x"

    assert VideoChunk(start_ms=0, end_ms=1, segment_summary="snake").segment_summary == "snake"
    assert VideoChunk(startTime=0, endTime=1, segmentSummary="camel").segment_summary == "camel"
    assert VideoChunk(startTime=0, endTime=1, segmentSummary=None).segment_summary == ""
    assert VideoChunk(startTime=0, endTime=1).segment_summary == ""
    assert VideoChunk(
        start_ms=0,
        startTime=0,
        end_ms=1,
        endTime=1,
        segment_summary="x",
        segmentSummary="x",
    ).segment_summary == "x"


def test_all_alias_conflicts_are_rejected() -> None:
    conflicts = (
        lambda: VideoContext(source="v", user_goal="a", userGoal="b"),
        lambda: VideoSegment(start_ms=0, startMs=1, end_ms=2),
        lambda: VideoSegment(start_ms=0, end_ms=2, ocr_texts=["a"], ocrTexts=["b"]),
        lambda: VideoChunk(start_ms=0, startTime=1, end_ms=2),
        lambda: VideoChunk(start_ms=0, end_ms=2, segment_summary="a", segmentSummary="b"),
        lambda: VideoChunk(start_ms=0, end_ms=2, raw_segments=[], rawSegments=[{}]),
        lambda: ChunkSummary(segment_summary="a", segmentSummary="b"),
        lambda: VideoEvidenceHit(start_ms=0, startMs=1),
        lambda: VideoEvidenceHit(start_ms=0, end_ms=1, ocr_texts=["a"], ocrTexts=["b"]),
        lambda: VideoRetrievalIntent(semantic_query="a", semanticQuery="b"),
        lambda: VideoRetrievalIntent(visual_keywords=["a"], visualKeywords=["b"]),
        lambda: AnalysisEvidence(timestamp_ms=0, timestampMs=1),
        lambda: AgentPlan(understood_goal="a", understoodGoal="b"),
        lambda: CriticResult(
            passed=True,
            missing_requirements=["a"],
            missingRequirements=["b"],
        ),
        lambda: CriticResult(
            passed=True,
            unsupported_claims=["a"],
            unsupportedClaims=["b"],
        ),
        lambda: CriticResult(
            passed=True,
            required_timestamps=[1],
            requiredTimestamps=[2],
        ),
        lambda: ModeProfile(display_name="a", displayName="b"),
        lambda: ModeProfile(plan_instruction="a", planInstruction="b"),
        lambda: ModeProfile(execute_instruction="a", executeInstruction="b"),
        lambda: ModeProfile(critic_instruction="a", criticInstruction="b"),
        lambda: ModeProfile(
            required_section_keys=["a"],
            requiredSectionKeys=["b"],
        ),
    )
    for factory in conflicts:
        with pytest.raises(ValidationError):
            factory()


def test_equal_aliases_and_none_are_not_silently_overridden() -> None:
    # Equal dual values are accepted; a supplied None is normalized only on
    # the spelling that supplied it, so it cannot overwrite the other key.
    assert AgentPlan(understood_goal="x", understoodGoal="x").understood_goal == "x"
    assert ModeProfile(
        required_section_keys=["a"], requiredSectionKeys=["a"]
    ).required_section_keys == ("a",)
    assert VideoEvidenceHit(start_ms=0, end_ms=1, ocrTexts=None).ocr_texts == ()
    assert VideoRetrievalIntent(visualKeywords=None).visual_keywords == ()


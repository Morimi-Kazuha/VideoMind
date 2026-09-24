from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic")

from pydantic import ValidationError

from dovideo.domain import (
    ChunkSummary,
    VideoChunk,
    VideoContext,
    VideoEvidenceHit,
    VideoRetrievalIntent,
    VideoSegment,
)


def test_video_context_normalizes_and_keeps_nested_collections_immutable() -> None:
    frames = ["frame-1.jpg"]
    segments = [
        VideoSegment(
            start_ms=0,
            end_ms=60_000,
            transcript="  hello world  ",
            ocr_texts=["slide"],
            evidence_frames=frames,
        )
    ]
    context = VideoContext(source="video.mp4", user_goal="  summarize  ", segments=segments)

    frames.append("mutated-after-parse.jpg")
    segments.clear()
    assert context.user_goal == "summarize"
    assert context.segments[0].transcript == "hello world"
    assert context.segments[0].evidence_frames == ("frame-1.jpg",)
    assert context.transcript_text() == "hello world"
    assert context.transcriptText() == "hello world"


def test_video_context_java_alias_round_trip() -> None:
    payload = {
        "source": "video.mp4",
        "userGoal": "learn",
        "segments": [
            {
                "startMs": 1,
                "endMs": 2,
                "transcript": "t",
                "ocrTexts": ["o"],
                "evidenceFrames": ["f"],
            }
        ],
    }
    context = VideoContext.model_validate(payload)
    restored = VideoContext.model_validate(
        json.loads(context.model_dump_json(by_alias=True))
    )
    assert restored == context
    assert json.loads(context.model_dump_json(by_alias=True))["userGoal"] == "learn"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: VideoContext(source=" "),
        lambda: VideoSegment(start_ms=-1, end_ms=1),
        lambda: VideoSegment(start_ms=2, end_ms=2),
        lambda: VideoChunk(start_ms=3, end_ms=2),
    ],
)
def test_invalid_video_ranges_are_rejected(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_nullable_java_defaults() -> None:
    context = VideoContext(source="video.mp4", userGoal=None, segments=None)
    segment = VideoSegment(startMs=0, endMs=1, transcript=None, ocrTexts=None, evidenceFrames=None)
    chunk = VideoChunk(startTime=0, endTime=1, segmentSummary=None, keywords=None, rawSegments=None, embedding=None)
    summary = ChunkSummary(segmentSummary=None, keywords=None)
    assert context.user_goal == ""
    assert context.segments == ()
    assert segment.transcript == ""
    assert segment.ocr_texts == ()
    assert chunk.segment_summary == ""
    assert chunk.raw_segments == ()
    assert summary.keywords == ()


def test_chunk_accepts_python_and_java_time_spellings() -> None:
    java_chunk = VideoChunk(startTime=0, endTime=300_000)
    python_chunk = VideoChunk(start_time=0, end_time=300_000)
    millis_chunk = VideoChunk(start_ms=0, end_ms=300_000)
    assert java_chunk == python_chunk == millis_chunk
    assert java_chunk.startMs == 0
    assert java_chunk.endMs == 300_000


def test_retrieval_terms_are_trimmed_deduplicated_and_limited() -> None:
    terms = [" A ", "A", "", "  ", None] + [f"k{i}" for i in range(20)]
    intent = VideoRetrievalIntent(
        semanticQuery="  explain trees  ",
        keywords=terms,
        visualKeywords=[" code ", "code", " OCR "],
    )
    assert intent.semantic_query == "explain trees"
    assert intent.keywords == ("A", *tuple(f"k{i}" for i in range(15)))
    assert intent.visual_keywords == ("code", "OCR")


def test_evidence_hit_preserves_java_permissive_range_behavior() -> None:
    hit = VideoEvidenceHit(startMs=-5, endMs=-1, source=None, snippet=None, transcript=None, ocrTexts=None)
    assert hit.start_ms == -5
    assert hit.end_ms == -1
    assert hit.source == ""
    assert hit.ocr_texts == ()


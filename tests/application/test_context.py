from __future__ import annotations

import pytest

from dovideo.application import (
    BothObservationBranchesFailed,
    EmptyVideoContextError,
    MediaObservationBundle,
    OcrBranchOutcome,
    OcrObservation,
    TranscriptSpan,
    VideoContextBuilder,
    build_video_context,
)
from dovideo.application.value_objects import AsrBranchOutcome


def bundle(
    *,
    transcripts: tuple[TranscriptSpan, ...] = (),
    ocr: tuple[OcrObservation, ...] = (),
    asr_causes: tuple[Exception, ...] = (),
    ocr_causes: tuple[Exception, ...] = (),
) -> MediaObservationBundle:
    asr = AsrBranchOutcome(
        observations=transcripts,
        attempted=len(transcripts) + len(asr_causes),
        failed=len(asr_causes),
        causes=asr_causes,
    )
    ocr_outcome = OcrBranchOutcome(
        observations=ocr,
        attempted=len(ocr) + len(ocr_causes),
        failed=len(ocr_causes),
        causes=ocr_causes,
    )
    return MediaObservationBundle(asr=asr, ocr=ocr_outcome)


def test_java_window_boundaries_and_exact_segment_shape() -> None:
    context = build_video_context(
        "memory://video",
        "  explain this ",
        bundle(
            transcripts=(
                TranscriptSpan(59_999, 60_000, "before boundary"),
                TranscriptSpan(0, 1, "at zero"),
                TranscriptSpan(60_000, 60_001, "next window"),
            ),
        ),
    )

    assert context.source == "memory://video"
    assert context.user_goal == "explain this"
    assert [(s.start_ms, s.end_ms) for s in context.segments] == [
        (0, 60_000),
        (60_000, 120_000),
    ]
    assert context.segments[0].transcript == "before boundary\nat zero"
    assert context.segments[1].transcript == "next window"


def test_mixed_branches_are_sorted_by_window_and_preserve_input_order_duplicates() -> None:
    context = VideoContextBuilder().build(
        "source",
        None,
        bundle(
            transcripts=(
                TranscriptSpan(60_001, 60_002, "second"),
                TranscriptSpan(60_000, 60_001, "first"),
                TranscriptSpan(60_001, 60_002, "second"),
            ),
            ocr=(
                OcrObservation(60_100, "ocr-2", "frame-2"),
                OcrObservation(0, "ocr-0", "frame-0"),
                OcrObservation(60_100, "ocr-2", "frame-2"),
            ),
        ),
    )

    assert [segment.start_ms for segment in context.segments] == [0, 60_000]
    assert context.segments[0].transcript == ""
    assert context.segments[0].ocr_texts == ("ocr-0",)
    assert context.segments[0].evidence_frames == ("frame-0",)
    assert context.segments[1].transcript == "second\nfirst\nsecond"
    assert context.segments[1].ocr_texts == ("ocr-2", "ocr-2")
    assert context.segments[1].evidence_frames == ("frame-2", "frame-2")


def test_blank_ocr_still_creates_frame_only_segment() -> None:
    context = build_video_context(
        "video",
        "goal",
        bundle(ocr=(OcrObservation(60_000, "", "evidence/frame.jpg"),)),
    )

    segment = context.segments[0]
    assert segment.start_ms == 60_000
    assert segment.transcript == ""
    assert segment.ocr_texts == ()
    assert segment.evidence_frames == ("evidence/frame.jpg",)


def test_one_failed_branch_degrades_without_losing_healthy_observations() -> None:
    asr_failed = bundle(
        asr_causes=(RuntimeError("ASR unavailable"),),
        ocr=(OcrObservation(0, "screen", "frame"),),
    )
    context = build_video_context("video", "goal", asr_failed)
    assert len(context.segments) == 1
    assert context.segments[0].ocr_texts == ("screen",)

    ocr_failed = bundle(
        transcripts=(TranscriptSpan(0, 1, "speech"),),
        ocr_causes=(RuntimeError("OCR unavailable"),),
    )
    context = build_video_context("video", "goal", ocr_failed)
    assert context.transcript_text() == "speech"


def test_both_failed_is_rejected_with_branch_owned_causes() -> None:
    asr_error = RuntimeError("asr")
    ocr_error = OSError("ocr")
    with pytest.raises(BothObservationBranchesFailed) as caught:
        build_video_context(
            "video",
            "goal",
            bundle(asr_causes=(asr_error,), ocr_causes=(ocr_error,)),
        )

    failure = caught.value
    assert failure.asr_causes == (asr_error,)
    assert failure.ocr_causes == (ocr_error,)
    assert failure.causes == (asr_error, ocr_error)


def test_empty_observations_are_rejected_but_java_blank_transcript_window_is_kept() -> None:
    with pytest.raises(EmptyVideoContextError):
        build_video_context("video", "goal", bundle())

    blank_context = build_video_context(
        "video",
        "goal",
        bundle(transcripts=(TranscriptSpan(0, 1, " "),)),
    )
    assert len(blank_context.segments) == 1
    assert blank_context.segments[0].transcript == ""

    with pytest.raises(EmptyVideoContextError):
        build_video_context("video", "goal", bundle(ocr=(OcrObservation(0, "", None),)))


def test_context_uses_stable_java_aliases_and_round_trips() -> None:
    context = build_video_context(
        "source",
        "goal",
        bundle(
            transcripts=(TranscriptSpan(0, 1, "speech"),),
            ocr=(OcrObservation(0, "text", "frame"),),
        ),
    )

    payload = context.model_dump_json()
    assert '"userGoal":"goal"' in payload
    assert '"startMs":0' in payload
    assert '"endMs":60000' in payload
    assert '"ocrTexts":["text"]' in payload
    assert '"evidenceFrames":["frame"]' in payload
    assert type(context).model_validate_json(payload) == context


def test_builder_does_not_mutate_input_collections_and_window_is_fixed() -> None:
    transcripts = [TranscriptSpan(0, 1, "one"), TranscriptSpan(0, 1, "two")]
    frames = [OcrObservation(0, "ocr", "frame")]
    observations = MediaObservationBundle(
        AsrBranchOutcome(observations=transcripts, attempted=2),  # type: ignore[arg-type]
        OcrBranchOutcome(observations=frames, attempted=1),  # type: ignore[arg-type]
    )
    before_transcripts = tuple(transcripts)
    before_frames = tuple(frames)

    context = VideoContextBuilder().build("video", "goal", observations)

    assert tuple(transcripts) == before_transcripts
    assert tuple(frames) == before_frames
    assert context.segments[0].transcript == "one\ntwo"
    with pytest.raises(ValueError):
        VideoContextBuilder(window_ms=30_000)

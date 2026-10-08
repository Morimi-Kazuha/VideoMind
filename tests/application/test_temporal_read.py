from __future__ import annotations

from dovideo.application.temporal_read import temporal_window_page, verified_answer_citations
from dovideo.application.temporal_read import verified_answer_presentation
from dovideo.application.context import VideoContextBuilder
from dovideo.application.evidence import bind_evidence_provenance
from dovideo.application.value_objects import (
    AsrBranchOutcome,
    MediaObservationBundle,
    OcrBranchOutcome,
    TranscriptSpan,
)
from dovideo.domain.agent import AgentState
from dovideo.domain.analysis import AnalysisEvidence, AnalysisResult
from dovideo.domain.video import VideoContext, VideoSegment


def test_empty_and_legacy_temporal_windows() -> None:
    assert temporal_window_page(None, limit=10, offset=0)["available"] is False
    context = VideoContext(
        source="media.mp4",
        segments=(
            VideoSegment(startMs=60000, endMs=120000, transcript="late"),
            VideoSegment(startMs=0, endMs=60000, transcript="early", ocrTexts=["slide"]),
        ),
    )
    page = temporal_window_page(context, limit=1, offset=0)
    assert page["total"] == 2
    assert page["items"][0]["startMs"] == 0
    assert page["items"][0]["ocrTexts"] == ["slide"]


def test_unbound_or_invalid_citations_cannot_become_trusted_links() -> None:
    context = VideoContext(
        source="media.mp4",
        segments=(VideoSegment(startMs=0, endMs=60000, transcript="source"),),
    )
    state = AgentState(
        goal="summarize",
        result=AnalysisResult(
            conclusions=["claim"],
            evidence=[
                AnalysisEvidence(timestampMs=0, source="ASR", content="source", claim="claim"),
                AnalysisEvidence(
                    timestampMs=0,
                    source="ASR",
                    content="source",
                    claim="claim",
                    sourceRevision="fake",
                    segmentId="fake",
                    sourceItemIds=["fake"],
                ),
            ],
        ),
    )
    assert verified_answer_citations(context, state) == []
    assert verified_answer_citations(context, None) == []


def test_valid_answer_evidence_exposes_verified_source_identity() -> None:
    context = VideoContextBuilder().build(
        "memory://media",
        "summarize",
        MediaObservationBundle(
            asr=AsrBranchOutcome(
                observations=(TranscriptSpan(0, 5000, "真实语音"),), attempted=1
            ),
            ocr=OcrBranchOutcome(observations=(), attempted=0),
        ),
        media_content_identity="sha256:sample",
    )
    result = AnalysisResult(
        conclusions=["结论"],
        evidence=[AnalysisEvidence(timestampMs=0, source="ASR", content="真实语音", claim="结论")],
    )
    bound = bind_evidence_provenance(context, result)
    citations = verified_answer_citations(context, AgentState(goal="summarize", result=bound))
    assert len(citations) == 1
    assert citations[0]["segmentId"] == context.segments[0].segment_id
    assert citations[0]["sourceItemIds"] == [context.segments[0].source_items[0].source_item_id]
    assert citations[0]["timestampMs"] == 0


def test_claim_presentation_keeps_missing_claims_and_rejects_wrong_provenance() -> None:
    context = VideoContextBuilder().build(
        "memory://media", "summarize",
        MediaObservationBundle(
            asr=AsrBranchOutcome(observations=(TranscriptSpan(0, 5000, "真实语音"),), attempted=1),
            ocr=OcrBranchOutcome(observations=(), attempted=0),
        ), media_content_identity="sha256:sample",
    )
    evidence = AnalysisEvidence(timestampMs=0, source="ASR", content="真实语音", claim="结论")
    bound = bind_evidence_provenance(context, AnalysisResult(
        conclusions=["结论", "缺失引用", "相似结论"], evidence=[evidence, evidence],
    ))
    invalid = [bound.evidence[0].model_copy(update=change) for change in (
        {"source_revision": "wrong"}, {"source_item_ids": ("fake",)},
        {"timestamp_ms": 5001}, {"claim": "其他结论"}, {"content": "历史伪造事实"},
    )]
    state = AgentState(goal="summarize", result=bound.model_copy(
        update={"evidence": (*bound.evidence, *invalid)},
    ))
    page = verified_answer_presentation(context, state)
    assert page["conclusions"] == ["结论", "缺失引用", "相似结论"]
    assert [item["claim"] for item in page["citations"]] == ["结论", "结论"]
    assert page["sourceRevision"] == context.source_revision
    changed = context.model_copy(update={"source_revision": "new"})
    assert verified_answer_presentation(changed, state)["citations"] == []
    assert verified_answer_presentation(None, None) == {
        "sourceRevision": "", "conclusions": [], "citations": [],
    }

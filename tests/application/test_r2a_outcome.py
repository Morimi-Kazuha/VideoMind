"""Contract-only proposals cannot mint video verification receipts."""
import pytest
from pydantic import ValidationError
from dovideo.application.follow_up_outcome import (
    AnswerKind, DirectFollowUpAnswer, FollowUpOutcome, KnowledgeBlock,
    MixedFollowUpAnswer, direct_outcome, mixed_outcome, render_outcome, video_outcome,
)
from dovideo.application.evidence import EvidenceVerificationService
from dovideo.domain import AnalysisEvidence, GroundedFollowUpAnswer
from test_p3_grounded_follow_up import _answer, _context


def ref(answer=None):
    item = (answer or _answer()).evidence[0]
    return AnalysisEvidence(timestamp_ms=item.timestamp_ms, source=item.source,
                            content=item.content, claim=item.claim)


def video(answer=None, evidence=None):
    answer = answer or _answer()
    return video_outcome(answer, "原有 Markdown 字符串", (evidence or ref(answer),),
                         _context(), EvidenceVerificationService())


def test_video_retains_text_and_bounded_citation_summary():
    outcome = video()
    assert render_outcome(outcome) == "原有 Markdown 字符串"
    assert outcome.answer_kind == AnswerKind.VIDEO_GROUNDED
    assert outcome.source_summary.model_dump() == {
        "video_evidence_count": 1, "video_source_types": ("ASR",),
        "knowledge_block_count": 0, "knowledge_kinds": (),
    }


def test_revision_alone_and_unrelated_source_cannot_verify_video():
    with pytest.raises(ValueError):
        video(evidence=ref().model_copy(update={"content": "Redis 使用磁盘", "source_revision": "rev"}))
    with pytest.raises(ValueError):
        video(evidence=ref().model_copy(update={"timestamp_ms": 999999}))


@pytest.mark.parametrize("text", ["[00:10] ASR 引用", "作者说缓存更快", "视频中出现 Redis",
                                 "视频证据", "sourceItemId=fake", "  ", "In this video Redis is faster."])
def test_knowledge_rejects_video_claims_and_timestamp_syntax(text):
    with pytest.raises(ValidationError):
        DirectFollowUpAnswer(answer=text)


def test_direct_rejects_evidence_fields_and_has_no_video_summary():
    with pytest.raises(ValidationError):
        DirectFollowUpAnswer(answer="缓存常用于减少访问延迟。", evidence=[])
    outcome = direct_outcome(DirectFollowUpAnswer(answer="缓存常用于减少访问延迟。"))
    assert render_outcome(outcome) == "缓存常用于减少访问延迟。"
    assert outcome.source_summary.video_evidence_count == 0


def test_mixed_keeps_video_knowledge_and_inference_separate():
    proposal = MixedFollowUpAnswer(video=_answer(), knowledge_blocks=(
        KnowledgeBlock(kind="MODEL_KNOWLEDGE", text="缓存常用于减少访问延迟。"),
        KnowledgeBlock(kind="INFERENCE", text="可能需要额外评估内存容量。"),
    ))
    result = mixed_outcome(proposal, "已核验视频块", (ref(),), _context(), EvidenceVerificationService())
    assert result.answer_kind == AnswerKind.MIXED
    assert "## 视频事实\n\n已核验视频块" in render_outcome(result)
    assert "通用知识：" in result.rendered_text and "推论：" in result.rendered_text
    assert result.source_summary.knowledge_kinds == ("MODEL_KNOWLEDGE", "INFERENCE")


@pytest.mark.parametrize("kind", [None, "UNKNOWN", "LEGACY_UNKNOWN"])
def test_illegal_outcome_kind_cannot_be_declared_verified(kind):
    payload = video().model_dump()
    payload["answer_kind"] = kind
    with pytest.raises(ValidationError):
        FollowUpOutcome.model_validate(payload)


def test_construct_copy_deserialization_and_mutation_do_not_bypass_receipt():
    result = video()
    for fake in (
        FollowUpOutcome.model_construct(**result.model_dump()),
        FollowUpOutcome.model_validate_json(result.model_dump_json()),
        result.model_copy(update={"rendered_text": "模型任意声称已核验"}),
    ):
        with pytest.raises(ValueError):
            render_outcome(fake)
    with pytest.raises(ValueError):
        direct_outcome(DirectFollowUpAnswer.model_construct(answer="[00:01] ASR"))
    with pytest.raises(ValueError):
        video(GroundedFollowUpAnswer.model_construct(answer="无引用", evidence=()), evidence=ref())


def test_original_grounded_dto_still_requires_at_least_one_evidence():
    with pytest.raises(ValidationError):
        GroundedFollowUpAnswer(answer="无引用", evidence=[])

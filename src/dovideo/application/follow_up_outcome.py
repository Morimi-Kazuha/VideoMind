"""Application-owned answer provenance. Generative DTOs are never receipts.

Only factories seal an outcome after validating its blocks. Video factories
require the current trusted context and the existing Evidence Guard. A copy or
model_construct cannot turn a model-declared source label into verification.
"""
from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictInt, StrictStr, field_validator, model_validator

from dovideo.domain import AnalysisEvidence, GroundedFollowUpAnswer, VideoContext
from .evidence import EvidenceVerificationService, normalize_evidence_text


class AnswerKind(str, Enum):
    VIDEO_GROUNDED = "VIDEO_GROUNDED"
    MODEL_KNOWLEDGE = "MODEL_KNOWLEDGE"
    MIXED = "MIXED"


class OutcomeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


AnswerText = Annotated[StrictStr, Field(min_length=1, max_length=4000)]


class KnowledgeBlock(OutcomeDTO):
    kind: Literal["MODEL_KNOWLEDGE", "INFERENCE"]
    text: AnswerText

    @field_validator("text")
    @classmethod
    def no_video_claims_or_citations(cls, value):
        if not value.strip() or re.search(
            r"\d{1,3}:\d{2}|视频(?:证据|中|里|画面|字幕)|作者(?:说|讲|认为)|"
            r"\b(?:in|from) (?:this |the )?video\b|\b(?:author|narrator) (?:says|said|states)\b|"
            r"\b(?:timestampMs|sourceItemId|candidateIndex|ASR|OCR)\b|"
            r"(?:作者|讲者|讲师)(?:声称|表示|指出|提到)|视频(?:显示|展示|提到)|"
            r"\b(?:speaker|lecturer|author|narrator) (?:claims|shows|explains|believes)\b|"
            r"\]\([^\n)]*(?:#t=|sourceItem|timestamp)[^\n)]*\)", value, re.I,
        ):
            raise ValueError("knowledge block cannot claim video evidence")
        return value


class DirectFollowUpAnswer(OutcomeDTO):
    answer: AnswerText

    @field_validator("answer")
    @classmethod
    def independent_answer(cls, value):
        return KnowledgeBlock(kind="MODEL_KNOWLEDGE", text=value).text


class MixedFollowUpAnswer(OutcomeDTO):
    # This is an unverified proposal, not a video-verification certificate.
    video: GroundedFollowUpAnswer
    knowledge_blocks: Annotated[tuple[KnowledgeBlock, ...], Field(min_length=1, max_length=4)]


class SourceSummary(OutcomeDTO):
    video_evidence_count: Annotated[StrictInt, Field(ge=0, le=5)]
    video_source_types: Annotated[tuple[Literal["ASR", "OCR", "ASR+OCR"], ...], Field(max_length=3)]
    knowledge_block_count: Annotated[StrictInt, Field(ge=0, le=4)]
    knowledge_kinds: Annotated[tuple[Literal["MODEL_KNOWLEDGE", "INFERENCE"], ...], Field(max_length=2)]


_SEAL = object()


class FollowUpOutcome(OutcomeDTO):
    answer_kind: AnswerKind
    rendered_text: Annotated[StrictStr, Field(min_length=1, max_length=24000)]
    source_summary: SourceSummary
    _seal: object | None = PrivateAttr(default=None)
    _digest: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def source_shape(self):
        s = self.source_summary
        video = self.answer_kind != AnswerKind.MODEL_KNOWLEDGE
        knowledge = self.answer_kind != AnswerKind.VIDEO_GROUNDED
        if (bool(s.video_evidence_count) != video or bool(s.video_source_types) != video
                or bool(s.knowledge_block_count) != knowledge or bool(s.knowledge_kinds) != knowledge):
            raise ValueError("inconsistent outcome provenance")
        return self


def _digest(outcome):
    return hashlib.sha256(outcome.model_dump_json().encode()).hexdigest()


def _seal(outcome):
    outcome._seal = _SEAL
    outcome._digest = _digest(outcome)
    return outcome


def render_outcome(outcome: FollowUpOutcome) -> str:
    checked = FollowUpOutcome.model_validate(outcome.model_dump(warnings=False))
    if outcome._seal is not _SEAL or outcome._digest != _digest(checked):
        raise ValueError("outcome lacks an unchanged application verification receipt")
    return checked.rendered_text


def video_outcome(answer: GroundedFollowUpAnswer, rendered_text: str,
                  evidence: tuple[AnalysisEvidence, ...], context: VideoContext,
                  verifier: EvidenceVerificationService) -> FollowUpOutcome:
    answer = GroundedFollowUpAnswer.model_validate(answer.model_dump())
    refs = tuple(AnalysisEvidence.model_validate(item.model_dump()) for item in evidence)
    if len(refs) != len(answer.evidence) or not refs:
        raise ValueError("missing verified video block")
    for item, proposed in zip(refs, answer.evidence, strict=True):
        if (item.timestamp_ms != proposed.timestamp_ms or item.source != proposed.source
                or item.content != proposed.content or item.claim != proposed.claim
                or not verifier.supported(context, item)
                or normalize_evidence_text(item.claim) not in normalize_evidence_text(answer.answer)):
            raise ValueError("unverified video block")
    # Production passes the existing renderer verbatim; provenance records
    # citation checks, not a new assertion of semantic entailment for all prose.
    return _seal(FollowUpOutcome(answer_kind=AnswerKind.VIDEO_GROUNDED, rendered_text=rendered_text,
        source_summary=SourceSummary(video_evidence_count=len(refs),
            video_source_types=tuple(dict.fromkeys(item.source for item in refs)),
            knowledge_block_count=0, knowledge_kinds=())))


def direct_outcome(answer: DirectFollowUpAnswer) -> FollowUpOutcome:
    answer = DirectFollowUpAnswer.model_validate(answer.model_dump(warnings=False))
    return _seal(FollowUpOutcome(answer_kind=AnswerKind.MODEL_KNOWLEDGE, rendered_text=answer.answer,
        source_summary=SourceSummary(video_evidence_count=0, video_source_types=(),
                                    knowledge_block_count=1, knowledge_kinds=("MODEL_KNOWLEDGE",))))


def mixed_outcome(proposal: MixedFollowUpAnswer, rendered_video: str,
                  evidence: tuple[AnalysisEvidence, ...], context: VideoContext,
                  verifier: EvidenceVerificationService) -> FollowUpOutcome:
    proposal = MixedFollowUpAnswer.model_validate(proposal.model_dump(warnings=False))
    video = video_outcome(proposal.video, rendered_video, evidence, context, verifier)
    text = "## 视频事实\n\n" + render_outcome(video) + "\n\n## 通用知识与推理\n\n" + "\n\n".join(
        ("推论：" if block.kind == "INFERENCE" else "通用知识：") + block.text for block in proposal.knowledge_blocks)
    return _seal(FollowUpOutcome(answer_kind=AnswerKind.MIXED, rendered_text=text,
        source_summary=SourceSummary(video_evidence_count=video.source_summary.video_evidence_count,
            video_source_types=video.source_summary.video_source_types,
            knowledge_block_count=len(proposal.knowledge_blocks),
            knowledge_kinds=tuple(dict.fromkeys(block.kind for block in proposal.knowledge_blocks)))))

"""Bounded structured DTOs for a grounded, same-video follow-up answer."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictInt, field_validator

from ._base import DomainModel


class GroundedFollowUpEvidence(DomainModel):
    """A model citation bound to one retrieved candidate and source channel."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        validate_default=True,
    )

    candidate_index: Annotated[
        StrictInt,
        Field(alias="candidateIndex", ge=0, le=7),
    ]
    timestamp_ms: Annotated[
        StrictInt,
        Field(alias="timestampMs", ge=0),
    ]
    source: Literal["ASR", "OCR", "ASR+OCR"]
    content: Annotated[str, Field(min_length=1, max_length=500)]
    claim: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("content", "claim")
    @classmethod
    def _require_nonblank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("follow-up evidence text must be nonblank")
        return normalized


class GroundedFollowUpAnswer(DomainModel):
    """One bounded answer with at least one source-verifiable citation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        validate_default=True,
    )

    answer: Annotated[str, Field(min_length=1, max_length=4_000)]
    evidence: Annotated[
        tuple[GroundedFollowUpEvidence, ...],
        Field(min_length=1, max_length=5),
    ]

    @field_validator("answer")
    @classmethod
    def _require_nonblank_answer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("follow-up answer must be nonblank")
        return normalized


__all__ = ["GroundedFollowUpAnswer", "GroundedFollowUpEvidence"]

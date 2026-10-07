"""Stable source provenance values for X2-A.

The provenance contract is deliberately small.  It names the authoritative
source revision and the deterministic identities derived from it, but it does
not introduce an evidence registry or an execution/event history.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from pydantic import Field, field_validator, model_validator

from ._base import DomainModel, normalize_nullable_aliases


LEGACY_PROVENANCE_VERSION = "x2-a-v1"
PROVENANCE_VERSION = "x2-a-v3"
EXTRACTION_CONTRACT_VERSION = "video-context-whisper-spans-v2"
NORMALIZATION_VERSION = "text-trim-v1"
CHUNKING_CONTRACT_VERSION = "video-chunk-5m-overlap1m-v2"
MAX_EVIDENCE_SOURCE_ITEM_REFS = 8


class SourceType(str, Enum):
    """Authoritative source branches that may support evidence."""

    ASR = "ASR"
    OCR = "OCR"


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """Descriptive value object for one deterministic source revision.

    Domain DTOs store the compact ``value`` string so old checkpoint payloads
    remain simple.  The additional fields make the inputs explicit for callers
    that need to inspect how a revision was calculated.
    """

    value: str
    media_identity: str = ""
    extraction_contract_version: str = EXTRACTION_CONTRACT_VERSION
    normalization_version: str = NORMALIZATION_VERSION
    observation_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or len(self.value) != 64:
            raise ValueError("source revision must be a SHA-256 hex digest")
        try:
            int(self.value, 16)
        except ValueError as error:
            raise ValueError("source revision must be a SHA-256 hex digest") from error

    @property
    def source_revision(self) -> str:
        return self.value

    def __str__(self) -> str:
        return self.value


class SegmentIdentity(DomainModel):
    """Stable identity metadata for one canonical temporal segment."""

    segment_id: str = Field(alias="segmentId")
    source_revision: str = Field(alias="sourceRevision")
    start_ms: int = Field(alias="startMs")
    end_ms: int = Field(alias="endMs")
    ordinal: int = 0
    provenance_version: str = Field(default=LEGACY_PROVENANCE_VERSION, alias="provenanceVersion")

    @field_validator("segment_id", "source_revision", "provenance_version")
    @classmethod
    def _require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provenance identity text is required")
        return normalized

    @model_validator(mode="after")
    def _valid_identity(self) -> "SegmentIdentity":
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("segment identity range is invalid")
        if self.ordinal < 0:
            raise ValueError("segment identity ordinal cannot be negative")
        return self


class SourceItemIdentity(DomainModel):
    """Stable identity for one ASR or OCR observation inside a segment."""

    source_item_id: str = Field(alias="sourceItemId")
    source_revision: str = Field(alias="sourceRevision")
    segment_id: str = Field(alias="segmentId")
    source_type: str = Field(alias="sourceType")
    ordinal: int
    timestamp_ms: int = Field(alias="timestampMs")
    end_ms: int | None = Field(default=None, alias="endMs")
    content_digest: str = Field(alias="contentDigest")
    frame_ref_digest: str = Field(default="", alias="frameRefDigest")
    provenance_version: str = Field(default=LEGACY_PROVENANCE_VERSION, alias="provenanceVersion")

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {
                "frame_ref_digest": "",
                "frameRefDigest": "",
                "provenance_version": LEGACY_PROVENANCE_VERSION,
                "provenanceVersion": LEGACY_PROVENANCE_VERSION,
                "end_ms": None,
                "endMs": None,
            },
            ("source_item_id", "sourceItemId"),
            ("source_revision", "sourceRevision"),
            ("segment_id", "segmentId"),
            ("source_type", "sourceType"),
            ("timestamp_ms", "timestampMs"),
            ("end_ms", "endMs"),
            ("content_digest", "contentDigest"),
            ("frame_ref_digest", "frameRefDigest"),
            ("provenance_version", "provenanceVersion"),
        )

    @field_validator(
        "source_item_id",
        "source_revision",
        "segment_id",
        "source_type",
        "content_digest",
        "frame_ref_digest",
        "provenance_version",
    )
    @classmethod
    def _trim_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized and value != "":
            raise ValueError("provenance identity text is required")
        return normalized

    @field_validator("source_type")
    @classmethod
    def _normalize_source_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {SourceType.ASR.value, SourceType.OCR.value}:
            raise ValueError("source_type must be ASR or OCR")
        return normalized

    @model_validator(mode="after")
    def _valid_identity(self) -> "SourceItemIdentity":
        if not self.source_item_id or not self.source_revision or not self.segment_id:
            raise ValueError("source item identity is incomplete")
        if not self.content_digest:
            raise ValueError("source item content digest is required")
        if self.ordinal < 0:
            raise ValueError("source item ordinal cannot be negative")
        if self.timestamp_ms < 0:
            raise ValueError("source item timestamp cannot be negative")
        if self.end_ms is not None and self.end_ms <= self.timestamp_ms:
            raise ValueError("source item range is invalid")
        return self


def canonical_source_observations(
    asr_observations: Iterable[Any],
    ocr_observations: Iterable[Any],
) -> dict[str, list[dict[str, Any]]]:
    """Return the canonical, ordered source-observation representation.

    Only JSON primitives are emitted.  Branch order and input order are
    explicit; mapping order, object repr, and Python object identity do not
    participate in the digest.
    """

    asr: list[dict[str, Any]] = []
    for ordinal, observation in enumerate(tuple(asr_observations)):
        asr.append(
            {
                "sourceType": SourceType.ASR.value,
                "ordinal": ordinal,
                "startMs": int(getattr(observation, "start_ms")),
                "endMs": int(getattr(observation, "end_ms")),
                "text": canonical_text(getattr(observation, "text", "")),
            }
        )

    ocr: list[dict[str, Any]] = []
    for ordinal, observation in enumerate(tuple(ocr_observations)):
        frame_ref = getattr(observation, "frame_ref", None)
        ocr.append(
            {
                "sourceType": SourceType.OCR.value,
                "ordinal": ordinal,
                "timestampMs": int(getattr(observation, "timestamp_ms")),
                "text": canonical_text(getattr(observation, "text", "")),
                "frameRef": canonical_frame_ref(frame_ref),
            }
        )
    return {"asr": asr, "ocr": ocr}


def canonical_source_observation_digest(
    asr_observations: Iterable[Any],
    ocr_observations: Iterable[Any],
) -> str:
    """Hash canonical source observations with SHA-256."""

    return sha256_canonical(
        canonical_source_observations(asr_observations, ocr_observations)
    )


def compute_source_revision(
    media_content_identity: str,
    asr_observations: Iterable[Any],
    ocr_observations: Iterable[Any],
    *,
    extraction_contract_version: str = EXTRACTION_CONTRACT_VERSION,
    normalization_version: str = NORMALIZATION_VERSION,
) -> str:
    """Calculate the authoritative source revision digest."""

    media_identity = canonical_text(media_content_identity)
    if not media_identity:
        raise ValueError("media content identity is required")
    observations = canonical_source_observations(asr_observations, ocr_observations)
    document = {
        "mediaIdentity": media_identity,
        "extractionContractVersion": extraction_contract_version,
        "normalizationVersion": normalization_version,
        "provenanceVersion": PROVENANCE_VERSION,
        "observations": observations,
    }
    return sha256_canonical(document)


def segment_id_for(
    source_revision: str,
    start_ms: int,
    end_ms: int,
    ordinal: int,
) -> str:
    """Create an ID in the source-revision namespace for one segment."""

    return "seg_" + sha256_canonical(
        {
            "sourceRevision": source_revision,
            "startMs": start_ms,
            "endMs": end_ms,
            "ordinal": ordinal,
            "provenanceVersion": PROVENANCE_VERSION,
        }
    )


def source_item_id_for(
    source_revision: str,
    segment_id: str,
    source_type: str,
    ordinal: int,
    timestamp_ms: int,
    end_ms: int | None,
    content: str,
    frame_ref: str | None = None,
) -> str:
    """Create a deterministic ID for one source observation."""

    return "item_" + sha256_canonical(
        {
            "sourceRevision": source_revision,
            "segmentId": segment_id,
            "sourceType": source_type.strip().upper(),
            "ordinal": ordinal,
            "timestampMs": timestamp_ms,
            "endMs": end_ms,
            "contentDigest": content_digest(content),
            "frameRefDigest": (
                content_digest(canonical_frame_ref(frame_ref)) if frame_ref else ""
            ),
            "provenanceVersion": PROVENANCE_VERSION,
        }
    )


def chunk_id_for(source_revision: str, start_ms: int, end_ms: int) -> str:
    """Create a retrieval-artifact ID in the source-revision namespace."""

    if not source_revision.strip():
        return ""
    return "chunk_" + sha256_canonical(
        {
            "sourceRevision": source_revision,
            "startMs": start_ms,
            "endMs": end_ms,
            "chunkingContractVersion": CHUNKING_CONTRACT_VERSION,
        }
    )


def content_digest(value: str | None) -> str:
    """Digest canonical text without storing the text in an identity."""

    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def canonical_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def canonical_frame_ref(value: Any) -> str:
    """Normalize reference spelling for hashing without persisting its path."""

    return canonical_text(value).replace("\\", "/")


def stable_frame_ref(media_identity: str, timestamp_ms: int, frame_index: int) -> str:
    """Name an extracted frame without including its temporary file location."""

    if not isinstance(media_identity, str) or not media_identity.strip():
        raise ValueError("stable media identity is required for frame provenance")
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int) or timestamp_ms < 0:
        raise ValueError("frame timestamp must be a nonnegative integer")
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
        raise ValueError("frame index must be a nonnegative integer")
    return "frame_" + sha256_canonical(
        {
            "mediaIdentity": media_identity.strip(),
            "timestampMs": timestamp_ms,
            "frameIndex": frame_index,
            "provenanceVersion": PROVENANCE_VERSION,
        }
    )


def sha256_canonical(value: Mapping[str, Any] | list[Any]) -> str:
    """Hash stable JSON with fixed separators, UTF-8, and sorted keys."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Compatibility spellings for callers that use ``calculate`` terminology.
calculate_source_revision = compute_source_revision
segment_id = segment_id_for
source_item_id = source_item_id_for


__all__ = [
    "CHUNKING_CONTRACT_VERSION",
    "EXTRACTION_CONTRACT_VERSION",
    "MAX_EVIDENCE_SOURCE_ITEM_REFS",
    "NORMALIZATION_VERSION",
    "PROVENANCE_VERSION",
    "SegmentIdentity",
    "SourceItemIdentity",
    "SourceRevision",
    "SourceType",
    "canonical_frame_ref",
    "stable_frame_ref",
    "canonical_source_observation_digest",
    "canonical_source_observations",
    "canonical_text",
    "calculate_source_revision",
    "chunk_id_for",
    "compute_source_revision",
    "content_digest",
    "segment_id",
    "segment_id_for",
    "sha256_canonical",
    "source_item_id",
    "source_item_id_for",
]

"""Immutable video-context and retrieval domain models.

The field aliases in this module are the JSON names emitted by the Java
records.  Python callers use snake_case names; both spellings are accepted on
input for migration-friendly deserialization.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from ._base import (
    DomainModel,
    normalize_nullable_aliases,
    reject_alias_conflicts,
    tuple_or_empty,
    value_or_empty,
)
from .provenance import SourceItemIdentity


class VideoSegment(DomainModel):
    """A time-window containing ASR, OCR, and evidence-frame references."""

    start_ms: Annotated[int, Field(default=0, alias="startMs")]
    end_ms: Annotated[int, Field(default=0, alias="endMs")]
    transcript: str = ""
    ocr_texts: Annotated[tuple[str, ...], Field(alias="ocrTexts")] = ()
    evidence_frames: Annotated[tuple[str, ...], Field(alias="evidenceFrames")] = ()
    source_revision: Annotated[str, Field(alias="sourceRevision")] = ""
    segment_id: Annotated[str, Field(alias="segmentId")] = ""
    source_items: Annotated[
        tuple[SourceItemIdentity, ...], Field(alias="sourceItems")
    ] = ()
    provenance_version: Annotated[str, Field(alias="provenanceVersion")] = ""

    @field_validator("start_ms", "end_ms")
    @classmethod
    def _require_integer_timestamp(cls, value: int) -> int:
        # The Java primitive is a long.  Pydantic validates the integer type;
        # this hook exists to keep a clear model-level error if a future
        # adapter supplies a non-integral value.
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable_collections(cls, data: Any) -> Any:
        normalized = reject_alias_conflicts(
            data,
            ("source_revision", "sourceRevision"),
            ("segment_id", "segmentId"),
            ("source_items", "sourceItems"),
            ("provenance_version", "provenanceVersion"),
            ("start_ms", "startMs"),
            ("end_ms", "endMs"),
            ("ocr_texts", "ocrTexts"),
            ("evidence_frames", "evidenceFrames"),
        )
        if isinstance(normalized, dict):
            normalized = dict(normalized)
            # Accept the descriptive ASR/OCR spellings as input while keeping
            # one serialized source_items collection as the canonical shape.
            if "source_items" not in normalized and "sourceItems" not in normalized:
                legacy_items: list[Any] = []
                for key in (
                    "asr_source_items",
                    "asrSourceItems",
                    "ocr_source_items",
                    "ocrSourceItems",
                ):
                    value = normalized.get(key)
                    if value is not None:
                        legacy_items.extend(tuple_or_empty(value))
                if legacy_items:
                    normalized["source_items"] = legacy_items
        return normalize_nullable_aliases(
            normalized,
            {
                "start_ms": 0,
                "startMs": 0,
                "end_ms": 0,
                "endMs": 0,
                "ocr_texts": (),
                "ocrTexts": (),
                "evidence_frames": (),
                "evidenceFrames": (),
                "transcript": "",
                "source_revision": "",
                "sourceRevision": "",
                "segment_id": "",
                "segmentId": "",
                "source_items": (),
                "sourceItems": (),
                "provenance_version": "",
                "provenanceVersion": "",
            },
            ("start_ms", "startMs"),
            ("end_ms", "endMs"),
            ("ocr_texts", "ocrTexts"),
            ("evidence_frames", "evidenceFrames"),
            ("source_revision", "sourceRevision"),
            ("segment_id", "segmentId"),
            ("source_items", "sourceItems"),
            ("provenance_version", "provenanceVersion"),
        )

    @field_validator("transcript")
    @classmethod
    def _trim_transcript(cls, value: str) -> str:
        return value.strip()

    @field_validator("ocr_texts", "evidence_frames", "source_items", mode="before")
    @classmethod
    def _copy_collections(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("source_revision", "segment_id", "provenance_version")
    @classmethod
    def _trim_provenance_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _valid_range(self) -> "VideoSegment":
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("invalid segment range")
        return self

    @property
    def start_time(self) -> int:
        """Compatibility spelling for code that uses the chunk terminology."""

        return self.start_ms

    @property
    def end_time(self) -> int:
        return self.end_ms

    @property
    def asr_source_items(self) -> tuple[SourceItemIdentity, ...]:
        return tuple(item for item in self.source_items if item.source_type == "ASR")

    @property
    def ocr_source_items(self) -> tuple[SourceItemIdentity, ...]:
        return tuple(item for item in self.source_items if item.source_type == "OCR")

    @property
    def source_item_ids(self) -> tuple[str, ...]:
        return tuple(item.source_item_id for item in self.source_items)


class VideoContext(DomainModel):
    """Unified time-ordered context consumed by retrieval and Agent roles."""

    source: str
    user_goal: str = Field(default="", alias="userGoal")
    segments: tuple[VideoSegment, ...] = ()
    source_revision: Annotated[str, Field(alias="sourceRevision")] = ""
    provenance_version: Annotated[str, Field(alias="provenanceVersion")] = ""

    @field_validator("source")
    @classmethod
    def _require_source(cls, value: str) -> str:
        # Java checks isBlank but deliberately does not trim the source.
        if not value.strip():
            raise ValueError("video source is required")
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {
                "user_goal": "",
                "userGoal": "",
                "source_revision": "",
                "sourceRevision": "",
                "provenance_version": "",
                "provenanceVersion": "",
            },
            ("user_goal", "userGoal"),
            ("source_revision", "sourceRevision"),
            ("provenance_version", "provenanceVersion"),
        )

    @field_validator("user_goal")
    @classmethod
    def _trim_goal(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_revision", "provenance_version")
    @classmethod
    def _trim_provenance_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("segments", mode="before")
    @classmethod
    def _copy_segments(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    def transcript_text(self) -> str:
        """Join non-blank transcript text in segment order."""

        return "\n".join(
            segment.transcript
            for segment in self.segments
            if segment.transcript.strip()
        )

    # Java callers expose this as a no-argument method.  The camelCase alias is
    # useful to migration adapters without changing the Python API.
    transcriptText = transcript_text


class VideoChunk(DomainModel):
    """A five-minute semantic chunk with summaries and raw source segments."""

    start_ms: Annotated[int, Field(default=0, alias="startTime")]
    end_ms: Annotated[int, Field(default=0, alias="endTime")]
    segment_summary: Annotated[str, Field(alias="segmentSummary")] = ""
    keywords: tuple[str, ...] = ()
    raw_segments: Annotated[tuple[VideoSegment, ...], Field(alias="rawSegments")] = ()
    embedding: tuple[float, ...] = ()
    chunk_id: Annotated[str, Field(alias="chunkId")] = ""
    source_revision: Annotated[str, Field(alias="sourceRevision")] = ""
    chunking_version: Annotated[str, Field(alias="chunkingVersion")] = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_java_and_python_time_spellings(cls, data: Any) -> Any:
        normalized = reject_alias_conflicts(
            data,
            ("start_ms", "startTime", "start_time"),
            ("end_ms", "endTime", "end_time"),
            ("segment_summary", "segmentSummary"),
            ("raw_segments", "rawSegments"),
            ("chunk_id", "chunkId"),
            ("source_revision", "sourceRevision"),
            ("chunking_version", "chunkingVersion"),
        )
        if not isinstance(normalized, dict):
            return normalized
        normalized = dict(normalized)
        # ``startTime``/``endTime`` are the Java record fields; ``start_time``
        # is accepted as the most literal Python translation, while
        # ``start_ms`` is the timestamp spelling used by VideoContext.
        for canonical, alternatives in (
            ("start_ms", ("start_time", "startTime")),
            ("end_ms", ("end_time", "endTime")),
        ):
            if canonical not in normalized:
                for alternative in alternatives:
                    if alternative in normalized:
                        normalized[canonical] = normalized[alternative]
                        break
        normalized = normalize_nullable_aliases(
            normalized,
            {
                "start_ms": 0,
                "startTime": 0,
                "start_time": 0,
                "end_ms": 0,
                "endTime": 0,
                "end_time": 0,
                "keywords": (),
                "raw_segments": (),
                "rawSegments": (),
                "embedding": (),
                "segment_summary": "",
                "segmentSummary": "",
                "chunk_id": "",
                "chunkId": "",
                "source_revision": "",
                "sourceRevision": "",
                "chunking_version": "",
                "chunkingVersion": "",
            },
            ("start_ms", "startTime", "start_time"),
            ("end_ms", "endTime", "end_time"),
            ("segment_summary", "segmentSummary"),
            ("raw_segments", "rawSegments"),
            ("chunk_id", "chunkId"),
            ("source_revision", "sourceRevision"),
            ("chunking_version", "chunkingVersion"),
        )
        return normalized

    @field_validator("segment_summary")
    @classmethod
    def _trim_summary(cls, value: str) -> str:
        return value.strip()

    @field_validator("keywords", "raw_segments", "embedding", mode="before")
    @classmethod
    def _copy_collections(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("chunk_id", "source_revision", "chunking_version")
    @classmethod
    def _trim_provenance_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _valid_range(self) -> "VideoChunk":
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("invalid chunk range")
        return self

    @property
    def start_time(self) -> int:
        return self.start_ms

    @property
    def end_time(self) -> int:
        return self.end_ms

    @property
    def startMs(self) -> int:  # noqa: N802 - Java migration property
        return self.start_ms

    @property
    def endMs(self) -> int:  # noqa: N802 - Java migration property
        return self.end_ms


class ChunkSummary(DomainModel):
    """LLM-produced summary used when building a :class:`VideoChunk`."""

    segment_summary: Annotated[str, Field(alias="segmentSummary")] = ""
    keywords: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {"segment_summary": "", "segmentSummary": "", "keywords": ()},
            ("segment_summary", "segmentSummary"),
        )

    @field_validator("segment_summary")
    @classmethod
    def _trim_summary(cls, value: str) -> str:
        return value.strip()

    @field_validator("keywords", mode="before")
    @classmethod
    def _copy_keywords(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)


class VideoEvidenceHit(DomainModel):
    """A directly seekable evidence hit returned by retrieval."""

    start_ms: Annotated[int, Field(default=0, alias="startMs")]
    end_ms: Annotated[int, Field(default=0, alias="endMs")]
    source: str = ""
    snippet: str = ""
    transcript: str = ""
    ocr_texts: Annotated[tuple[str, ...], Field(alias="ocrTexts")] = ()
    source_revision: Annotated[str, Field(alias="sourceRevision")] = ""
    chunk_id: Annotated[str, Field(alias="chunkId")] = ""
    segment_id: Annotated[str, Field(alias="segmentId")] = ""
    source_item_ids: Annotated[
        tuple[str, ...], Field(alias="sourceItemIds")
    ] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        normalized = reject_alias_conflicts(
            data,
            ("start_ms", "startMs"),
            ("end_ms", "endMs"),
            ("ocr_texts", "ocrTexts"),
            ("source_revision", "sourceRevision"),
            ("chunk_id", "chunkId"),
            ("segment_id", "segmentId"),
            ("source_item_ids", "sourceItemIds"),
        )
        return normalize_nullable_aliases(
            normalized,
            {
                "start_ms": 0,
                "startMs": 0,
                "end_ms": 0,
                "endMs": 0,
                "source": "",
                "snippet": "",
                "transcript": "",
                "ocr_texts": (),
                "ocrTexts": (),
                "source_revision": "",
                "sourceRevision": "",
                "chunk_id": "",
                "chunkId": "",
                "segment_id": "",
                "segmentId": "",
                "source_item_ids": (),
                "sourceItemIds": (),
            },
            ("start_ms", "startMs"),
            ("end_ms", "endMs"),
            ("ocr_texts", "ocrTexts"),
            ("source_revision", "sourceRevision"),
            ("chunk_id", "chunkId"),
            ("segment_id", "segmentId"),
            ("source_item_ids", "sourceItemIds"),
        )

    @field_validator("ocr_texts", "source_item_ids", mode="before")
    @classmethod
    def _copy_ocr(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("source_revision", "chunk_id", "segment_id")
    @classmethod
    def _trim_provenance_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_item_ids")
    @classmethod
    def _normalize_source_item_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return tuple(result)


class VideoRetrievalIntent(DomainModel):
    """Semantic and visual clues produced by the retrieval planner."""

    semantic_query: Annotated[str, Field(alias="semanticQuery")] = ""
    keywords: tuple[str, ...] = ()
    visual_keywords: Annotated[tuple[str, ...], Field(alias="visualKeywords")] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {
                "semantic_query": "",
                "semanticQuery": "",
                "visual_keywords": (),
                "visualKeywords": (),
            },
            ("semantic_query", "semanticQuery"),
            ("visual_keywords", "visualKeywords"),
        )

    @field_validator("semantic_query")
    @classmethod
    def _trim_query(cls, value: str) -> str:
        return value.strip()

    @field_validator("keywords", "visual_keywords", mode="before")
    @classmethod
    def _normalize_terms(cls, value: Any) -> tuple[str, ...]:
        values = tuple_or_empty(value)
        output: list[str] = []
        seen: set[str] = set()
        for term in values:
            if term is None:
                continue
            normalized = term.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            output.append(normalized)
            if len(output) == 16:
                break
        return tuple(output)


# Java uses nested records.  The Python models are top-level for ergonomic
# imports, while these attributes keep the migration spelling available.
VideoContext.VideoSegment = VideoSegment  # type: ignore[attr-defined]
VideoChunk.ChunkSummary = ChunkSummary  # type: ignore[attr-defined]


__all__ = [
    "ChunkSummary",
    "VideoChunk",
    "VideoContext",
    "VideoEvidenceHit",
    "VideoRetrievalIntent",
    "VideoSegment",
]

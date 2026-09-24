"""Structured analysis output models."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from ._base import (
    DomainModel,
    normalize_nullable_aliases,
    reject_alias_conflicts,
    tuple_or_empty,
)
from .provenance import MAX_EVIDENCE_SOURCE_ITEM_REFS


class AnalysisEvidence(DomainModel):
    """One timestamped source item supporting a claim."""

    timestamp_ms: Annotated[int, Field(default=0, alias="timestampMs")]
    source: str = "UNKNOWN"
    content: str = ""
    claim: str = ""
    source_revision: Annotated[str, Field(alias="sourceRevision")] = ""
    segment_id: Annotated[str, Field(alias="segmentId")] = ""
    source_item_id: Annotated[str, Field(alias="sourceItemId")] = ""
    source_item_ids: Annotated[
        tuple[str, ...], Field(alias="sourceItemIds")
    ] = ()
    source_provenance_version: Annotated[
        str, Field(alias="sourceProvenanceVersion")
    ] = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        normalized = reject_alias_conflicts(
            data,
            ("timestamp_ms", "timestampMs"),
            ("source_revision", "sourceRevision"),
            ("segment_id", "segmentId"),
            ("source_item_id", "sourceItemId"),
            ("source_item_ids", "sourceItemIds"),
            ("source_provenance_version", "sourceProvenanceVersion"),
        )
        if isinstance(normalized, dict):
            normalized = dict(normalized)
            if "source_item_ids" not in normalized and "sourceItemIds" not in normalized:
                singular = normalized.get("source_item_id", normalized.get("sourceItemId"))
                if singular:
                    normalized["source_item_ids"] = (singular,)
        normalized = normalize_nullable_aliases(
            normalized,
            {"timestamp_ms": 0, "timestampMs": 0},
            ("timestamp_ms", "timestampMs"),
            ("source_revision", "sourceRevision"),
            ("segment_id", "segmentId"),
            ("source_item_id", "sourceItemId"),
            ("source_item_ids", "sourceItemIds"),
            ("source_provenance_version", "sourceProvenanceVersion"),
        )
        if not isinstance(normalized, dict):
            return normalized
        normalized = dict(normalized)
        for key, default in (
            ("source", "UNKNOWN"),
            ("content", ""),
            ("claim", ""),
            ("source_revision", ""),
            ("segment_id", ""),
            ("source_item_id", ""),
            ("source_item_ids", ()),
            ("source_provenance_version", ""),
        ):
            if normalized.get(key) is None:
                normalized[key] = default
        return normalized

    @field_validator("source")
    @classmethod
    def _trim_source(cls, value: str) -> str:
        return value.strip()

    @field_validator(
        "content",
        "claim",
        "source_revision",
        "segment_id",
        "source_item_id",
        "source_provenance_version",
    )
    @classmethod
    def _trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_item_ids", mode="before")
    @classmethod
    def _copy_source_item_ids(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    @field_validator("source_item_ids")
    @classmethod
    def _normalize_source_item_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_EVIDENCE_SOURCE_ITEM_REFS:
            raise ValueError("too many evidence source item references")
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise ValueError("source item references must be strings")
            normalized = item.strip()
            if not normalized:
                raise ValueError("source item references must not be blank")
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return tuple(result)

    @model_validator(mode="after")
    def _nonnegative_timestamp(self) -> "AnalysisEvidence":
        if self.timestamp_ms < 0:
            raise ValueError("evidence timestamp cannot be negative")
        if self.source_item_id and self.source_item_ids:
            if self.source_item_id != self.source_item_ids[0]:
                raise ValueError("source_item_id must match the first source_item_ids entry")
        elif self.source_item_id:
            object.__setattr__(self, "source_item_ids", (self.source_item_id,))
        elif self.source_item_ids:
            object.__setattr__(self, "source_item_id", self.source_item_ids[0])
        return self


class AnalysisSection(DomainModel):
    """A mode-specific, machine-identifiable output section."""

    key: str = ""
    title: str = ""
    items: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for key in ("key", "title"):
            if normalized.get(key) is None:
                normalized[key] = ""
        if normalized.get("items") is None:
            normalized["items"] = ()
        return normalized

    @field_validator("key", "title")
    @classmethod
    def _trim_labels(cls, value: str) -> str:
        return value.strip()

    @field_validator("items", mode="before")
    @classmethod
    def _copy_items(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)


class AnalysisResult(DomainModel):
    """Fixed Agent output: title, conclusions, evidence, suggestions, sections."""

    title: str = "未命名分析"
    conclusions: tuple[str, ...] = ()
    evidence: tuple[AnalysisEvidence, ...] = ()
    suggestions: tuple[str, ...] = ()
    sections: tuple[AnalysisSection, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if normalized.get("title") is None:
            normalized["title"] = "未命名分析"
        for key in ("conclusions", "evidence", "suggestions", "sections"):
            if normalized.get(key) is None:
                normalized[key] = ()
        return normalized

    @field_validator("title")
    @classmethod
    def _trim_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("conclusions", "evidence", "suggestions", "sections", mode="before")
    @classmethod
    def _copy_collections(cls, value: Any) -> tuple[Any, ...]:
        return tuple_or_empty(value)

    def to_markdown(self) -> str:
        """Render the Java-compatible user-facing markdown representation."""

        result = [f"## {self.title}\n\n## 核心结论\n"]
        result.extend(f"- {item}\n" for item in self.conclusions)
        result.append("\n## 视频证据\n")
        result.extend(
            f"- [{self._format_time(item.timestamp_ms)}] {item.source}：{item.content}\n"
            for item in self.evidence
        )
        result.append("\n## 建议\n")
        result.extend(f"- {item}\n" for item in self.suggestions)
        for section in self.sections:
            result.append(f"\n## {section.title}\n")
            result.extend(f"- {item}\n" for item in section.items)
        return "".join(result)

    # Java naming retained as an adapter method for checkpoint/render callers.
    toMarkdown = to_markdown

    @staticmethod
    def _format_time(timestamp_ms: int) -> str:
        seconds = timestamp_ms // 1000
        return f"{seconds // 60:02d}:{seconds % 60:02d}"


# Java AnalysisResult declares these as nested records.  Keep the nested
# spelling available in addition to the Python-native top-level names.
AnalysisResult.Evidence = AnalysisEvidence  # type: ignore[attr-defined]
AnalysisResult.Section = AnalysisSection  # type: ignore[attr-defined]


__all__ = ["AnalysisEvidence", "AnalysisResult", "AnalysisSection"]

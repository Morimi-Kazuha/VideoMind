"""Analysis modes and their prompt/section profile contract."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from ._base import DomainModel, normalize_nullable_aliases, tuple_or_empty


class AnalysisMode(str, Enum):
    """Supported Agent output paths."""

    GENERAL = "GENERAL"
    LEARNING = "LEARNING"
    REVIEW = "REVIEW"
    CREATION = "CREATION"

    @classmethod
    def from_nullable(cls, value: str | None) -> "AnalysisMode":
        """Parse a nullable/unknown value using Java's GENERAL fallback."""

        if value is None or not value.strip():
            return cls.GENERAL
        try:
            return cls[value.strip().upper()]
        except KeyError:
            return cls.GENERAL

    @classmethod
    def from_request(cls, value: str | None) -> "AnalysisMode":
        """Parse an HTTP request value; unknown explicit values are rejected."""

        if value is None or not value.strip():
            return cls.GENERAL
        try:
            return cls[value.strip().upper()]
        except KeyError as error:
            raise ValueError(f"不支持的分析模式: {value}") from error

    # Familiar migration names for adapters ported from the Java utility.
    fromNullable = from_nullable
    fromRequest = from_request


class ModeProfile(DomainModel):
    """Mode-specific instructions and required structured section keys."""

    mode: AnalysisMode | None = None
    display_name: Annotated[str, Field(alias="displayName")] = ""
    plan_instruction: Annotated[str, Field(alias="planInstruction")] = ""
    execute_instruction: Annotated[str, Field(alias="executeInstruction")] = ""
    critic_instruction: Annotated[str, Field(alias="criticInstruction")] = ""
    required_section_keys: Annotated[tuple[str, ...], Field(alias="requiredSectionKeys")] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {
                "display_name": "",
                "displayName": "",
                "plan_instruction": "",
                "planInstruction": "",
                "execute_instruction": "",
                "executeInstruction": "",
                "critic_instruction": "",
                "criticInstruction": "",
                "required_section_keys": (),
                "requiredSectionKeys": (),
            },
            ("display_name", "displayName"),
            ("plan_instruction", "planInstruction"),
            ("execute_instruction", "executeInstruction"),
            ("critic_instruction", "criticInstruction"),
            ("required_section_keys", "requiredSectionKeys"),
        )

    @field_validator(
        "display_name",
        "plan_instruction",
        "execute_instruction",
        "critic_instruction",
    )
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        # ModeProfile's Java constructor normalizes null only and does not
        # trim instruction text; display names/instructions may intentionally
        # contain leading spaces for prompt composition.
        return value

    @field_validator("required_section_keys", mode="before")
    @classmethod
    def _normalize_keys(cls, value: Any) -> tuple[str, ...]:
        seen: set[str] = set()
        output: list[str] = []
        for key in tuple_or_empty(value):
            if key in seen:
                continue
            seen.add(key)
            output.append(key)
        return tuple(output)


__all__ = ["AnalysisMode", "ModeProfile"]

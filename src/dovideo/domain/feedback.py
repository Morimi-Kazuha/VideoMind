"""User feedback DTO used by the Phase 8C checkpoint service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Callable

from pydantic import Field, field_validator, model_validator

from ._base import DomainModel, normalize_nullable_aliases, tuple_or_empty
from .agent import java_string_length
from .modes import AnalysisMode


class AgentFeedback(DomainModel):
    """Java ``AgentFeedback`` record with a separate normalization step.

    Construction preserves the Java record's raw text (apart from validation);
    :meth:`normalized` trims text, maps nullable/unknown modes to GENERAL,
    filters blank corrected tasks, and fills a missing creation timestamp.
    """

    media_id: Annotated[int, Field(alias="mediaId")]
    goal: str
    mode: str | None = None
    rating: int | None = None
    error_type: Annotated[str | None, Field(alias="errorType")] = None
    comment: str | None = None
    corrected_goal: Annotated[str | None, Field(alias="correctedGoal")] = None
    corrected_tasks: Annotated[tuple[str | None, ...], Field(alias="correctedTasks")] = ()
    evidence_timestamp: Annotated[int | None, Field(alias="evidenceTimestamp")] = None
    evidence_accepted: Annotated[bool | None, Field(alias="evidenceAccepted")] = None
    created_at: Annotated[datetime | None, Field(alias="createdAt")] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_nullable(cls, data: Any) -> Any:
        return normalize_nullable_aliases(
            data,
            {
                "mode": None,
                "error_type": None,
                "comment": None,
                "corrected_goal": None,
                "corrected_tasks": (),
                "evidence_timestamp": None,
                "evidenceTimestamp": None,
                "evidence_accepted": None,
                "created_at": None,
                "createdAt": None,
            },
            ("error_type", "errorType"),
            ("corrected_goal", "correctedGoal"),
            ("corrected_tasks", "correctedTasks"),
            ("evidence_timestamp", "evidenceTimestamp"),
            ("evidence_accepted", "evidenceAccepted"),
            ("created_at", "createdAt"),
        )

    @field_validator("media_id")
    @classmethod
    def _require_media_id(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("mediaId is required")
        return value

    @field_validator("goal")
    @classmethod
    def _validate_goal(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("analysis goal is required")
        if java_string_length(value) > 500:
            raise ValueError("analysis goal cannot exceed 500 UTF-16 code units")
        return value

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str | None) -> str | None:
        if value is not None and not isinstance(value, str):
            raise TypeError("mode must be text or null")
        return value

    @staticmethod
    def _bounded_text(value: str | None, limit: int, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(f"{name} must be text or null")
        if java_string_length(value) > limit:
            raise ValueError(f"{name} cannot exceed {limit} UTF-16 code units")
        return value

    @field_validator("error_type")
    @classmethod
    def _error_type_size(cls, value: str | None) -> str | None:
        return cls._bounded_text(value, 64, "errorType")

    @field_validator("comment")
    @classmethod
    def _comment_size(cls, value: str | None) -> str | None:
        return cls._bounded_text(value, 2000, "comment")

    @field_validator("corrected_goal")
    @classmethod
    def _corrected_goal_size(cls, value: str | None) -> str | None:
        return cls._bounded_text(value, 500, "correctedGoal")

    @field_validator("corrected_tasks", mode="before")
    @classmethod
    def _corrected_tasks_size(cls, value: Any) -> tuple[str | None, ...]:
        values = tuple_or_empty(value)
        if len(values) > 5:
            raise ValueError("correctedTasks cannot contain more than five tasks")
        checked: list[str | None] = []
        for task in values:
            if task is not None and not isinstance(task, str):
                raise TypeError("correctedTasks values must be text or null")
            if task is not None and java_string_length(task) > 500:
                raise ValueError("each corrected task cannot exceed 500 UTF-16 code units")
            checked.append(task)
        return tuple(checked)

    @field_validator("evidence_timestamp")
    @classmethod
    def _timestamp_nonnegative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("evidence timestamp cannot be negative")
        return value

    def normalized(
        self,
        mode: AnalysisMode | str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        now: datetime | None = None,
    ) -> "AgentFeedback":
        """Return Java's normalized feedback record.

        ``clock`` is preferred for deterministic tests.  ``now`` is a useful
        one-shot injection; otherwise an aware UTC timestamp is generated.
        """

        selected_mode = AnalysisMode.from_nullable(self.mode if mode is None else mode)
        created_at = self.created_at
        if created_at is None:
            created_at = now if now is not None else clock() if clock is not None else datetime.now(timezone.utc)
        tasks = tuple(
            task.strip()
            for task in self.corrected_tasks
            if task is not None and task.strip()
        )
        return AgentFeedback(
            mediaId=self.media_id,
            goal=self.goal.strip(),
            mode=selected_mode.name,
            rating=self.rating,
            errorType=None if self.error_type is None else self.error_type.strip(),
            comment=None if self.comment is None else self.comment.strip(),
            correctedGoal=(
                None if self.corrected_goal is None else self.corrected_goal.strip()
            ),
            correctedTasks=tasks,
            evidenceTimestamp=self.evidence_timestamp,
            evidenceAccepted=self.evidence_accepted,
            createdAt=created_at,
        )

    normalize = normalized
    normalised = normalized


__all__ = ["AgentFeedback"]

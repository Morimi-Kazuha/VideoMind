"""Durable, provider-neutral dead-letter handoff records.

The worker must be able to finish a dead-letter publication after its
process has gone away.  This record therefore contains only the stable task
identity, the small request metadata needed to rebuild an
``AnalysisRequest``, and a safe description of the original failure.  It
never serializes an exception object or imports a class named by persisted
data.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, field_validator

from dovideo.domain import AnalysisMode
from dovideo.domain._base import DomainModel

from .value_objects import AnalysisRequest, MediaRef, TaskKey


class PersistedDeadLetterError(RuntimeError):
    """Safe reconstructed error used for a later dead-letter publication."""

    def __init__(self, error_type: str, error_message: str) -> None:
        self.error_type = str(error_type)
        self.error_message = str(error_message)
        message = f"{self.error_type}: {self.error_message}"
        super().__init__(message)


class PendingDeadLetterHandoff(DomainModel):
    """JSON-safe pending dead-letter delivery.

    Field aliases intentionally follow the Java/Jackson checkpoint payload
    names.  The bounded text fields prevent a provider exception from
    turning a retry record into an unbounded persistence payload.
    """

    media_id: Annotated[int, Field(alias="mediaId")]
    goal: str
    mode: AnalysisMode = AnalysisMode.GENERAL
    source: str
    filename: str | None = None
    content_hash: Annotated[str | None, Field(alias="contentHash")] = None
    status: str | None = None
    request_id: Annotated[str | None, Field(alias="requestId")] = None
    attempt: int
    error_type: Annotated[str, Field(alias="errorType")]
    error_message: Annotated[str, Field(alias="errorMessage")]

    @field_validator("media_id")
    @classmethod
    def _media_id_is_integer(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("mediaId must be an integer")
        return value

    @field_validator("goal", "source", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("pending handoff text is required")
        return value.strip()

    @field_validator("filename", "content_hash", "status", "request_id", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("pending handoff optional text must be text")
        return value

    @field_validator("attempt")
    @classmethod
    def _attempt_is_positive(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("pending handoff attempt must be positive")
        return value

    @field_validator("error_type", mode="before")
    @classmethod
    def _error_type_text(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("pending handoff error type is required")
        value = value.strip()
        if len(value) > 128:
            raise ValueError("pending handoff error type is too long")
        return value

    @field_validator("error_message", mode="before")
    @classmethod
    def _error_message_text(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        if len(value) > 8192:
            return value[:8192]
        return value

    @property
    def task_key(self) -> TaskKey:
        return TaskKey(self.media_id, self.goal, self.mode)

    def to_request(self) -> AnalysisRequest:
        """Rebuild the application request without dynamic imports."""

        return AnalysisRequest(
            media=MediaRef(
                media_id=self.media_id,
                source=self.source,
                filename=self.filename,
                content_hash=self.content_hash,
                status=self.status,
            ),
            goal=self.goal,
            mode=self.mode,
            request_id=self.request_id,
        )

    @property
    def request(self) -> AnalysisRequest:
        return self.to_request()

    def to_exception(self) -> PersistedDeadLetterError:
        """Return a safe generic exception carrying persisted diagnostics."""

        return PersistedDeadLetterError(self.error_type, self.error_message)

    @classmethod
    def from_request(
        cls,
        request: AnalysisRequest,
        *,
        attempt: int,
        error: BaseException,
    ) -> "PendingDeadLetterHandoff":
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be an AnalysisRequest")
        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception")
        return cls(
            mediaId=request.media.media_id,
            goal=request.goal,
            mode=request.mode,
            source=request.media.source,
            filename=request.media.filename,
            contentHash=request.media.content_hash,
            status=request.media.status,
            requestId=request.request_id,
            attempt=attempt,
            errorType=type(error).__name__,
            errorMessage=str(error),
        )


DeadLetterHandoff = PendingDeadLetterHandoff


__all__ = [
    "DeadLetterHandoff",
    "PendingDeadLetterHandoff",
    "PersistedDeadLetterError",
]

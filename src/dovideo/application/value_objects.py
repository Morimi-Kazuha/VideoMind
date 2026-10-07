"""Small immutable values shared by application ports.

These values deliberately do not duplicate the Pydantic domain records.  They
identify an application request, a media source, or an adapter observation;
the domain package remains the source of truth for video/agent payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final

from dovideo.domain import AnalysisMode


@dataclass(frozen=True, slots=True)
class TaskKey:
    """Stable identity of one media/goal/mode analysis task."""

    media_id: int
    goal: str
    mode: AnalysisMode = AnalysisMode.GENERAL

    def __post_init__(self) -> None:
        if isinstance(self.media_id, bool) or not isinstance(self.media_id, int):
            raise TypeError("media_id must be an integer")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("analysis goal is required")
        object.__setattr__(self, "goal", self.goal.strip())
        object.__setattr__(self, "mode", _resolve_mode(self.mode))


@dataclass(frozen=True, slots=True)
class MediaRef:
    """Metadata needed to resolve a media record without importing an ORM."""

    media_id: int
    source: str
    filename: str | None = None
    content_hash: str | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.media_id, bool) or not isinstance(self.media_id, int):
            raise TypeError("media_id must be an integer")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("media source is required")
        if self.filename is not None and not isinstance(self.filename, str):
            raise TypeError("filename must be text or None")
        if self.content_hash is not None and not isinstance(self.content_hash, str):
            raise TypeError("content_hash must be text or None")
        if self.status is not None and not isinstance(self.status, str):
            raise TypeError("status must be text or None")

    @property
    def readable_source(self) -> str:
        """Source token passed to the dedicated readable-source port."""

        return self.source


@dataclass(frozen=True, slots=True)
class ReadableSource:
    """A provider-resolved source safe to hand to ASR/OCR adapters."""

    uri: str

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("readable source is required")


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """Input to dispatch/application workflows."""

    media: MediaRef
    goal: str
    mode: AnalysisMode = AnalysisMode.GENERAL
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.media, MediaRef):
            raise TypeError("media must be a MediaRef")
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("analysis goal is required")
        object.__setattr__(self, "goal", self.goal.strip())
        object.__setattr__(self, "mode", _resolve_mode(self.mode))
        if self.request_id is not None and (
            not isinstance(self.request_id, str) or not self.request_id.strip()
        ):
            raise ValueError("request_id must be nonblank when provided")

    @property
    def task_key(self) -> TaskKey:
        return TaskKey(self.media.media_id, self.goal, self.mode)


@dataclass(frozen=True, slots=True)
class TranscriptSpan:
    """ASR observation corresponding to Java ``TranscriptSegment``."""

    start_ms: int
    end_ms: int
    text: str = ""

    def __post_init__(self) -> None:
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("invalid transcript range")
        if self.text is None:  # type: ignore[comparison-overlap]
            object.__setattr__(self, "text", "")
        elif not isinstance(self.text, str):
            raise TypeError("transcript text must be text")
        else:
            object.__setattr__(self, "text", self.text.strip())


@dataclass(frozen=True, slots=True)
class OcrObservation:
    """One timestamped OCR result and its optional evidence-frame reference."""

    timestamp_ms: int
    text: str = ""
    frame_ref: str | None = None
    frame_location: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp_ms < 0:
            raise ValueError("OCR timestamp cannot be negative")
        if self.text is None:  # type: ignore[comparison-overlap]
            object.__setattr__(self, "text", "")
        elif not isinstance(self.text, str):
            raise TypeError("OCR text must be text")
        if self.frame_ref is not None and not isinstance(self.frame_ref, str):
            raise TypeError("frame_ref must be text or None")
        if self.frame_location is not None and not isinstance(self.frame_location, str):
            raise TypeError("frame_location must be text or None")


class BranchStatus(str, Enum):
    """Outcome state shared by the independent ASR and OCR branches."""

    SUCCESS = "SUCCESS"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AsrBranchOutcome:
    """Immutable ASR observations plus structured per-segment failures."""

    observations: tuple[TranscriptSpan, ...] = ()
    attempted: int = 0
    failed: int = 0
    causes: tuple[Exception, ...] = ()
    branch_error: Exception | None = None

    def __post_init__(self) -> None:
        observations = tuple(self.observations)
        causes = tuple(self.causes)
        if not all(isinstance(item, TranscriptSpan) for item in observations):
            raise TypeError("ASR observations must be TranscriptSpan values")
        if not all(isinstance(item, Exception) for item in causes):
            raise TypeError("ASR causes must be exceptions")
        if self.branch_error is not None and not isinstance(self.branch_error, Exception):
            raise TypeError("ASR branch_error must be an Exception or None")
        if not isinstance(self.attempted, int) or isinstance(self.attempted, bool):
            raise TypeError("ASR attempted count must be an integer")
        if self.attempted < 0:
            raise ValueError("ASR attempted count cannot be negative")
        if not isinstance(self.failed, int) or isinstance(self.failed, bool):
            raise TypeError("ASR failed count must be an integer")
        if self.failed < 0:
            raise ValueError("ASR failed count cannot be negative")
        if self.failed != len(causes):
            raise ValueError("ASR failed count must match causes")
        if self.attempted < self.failed:
            raise ValueError("ASR attempted count cannot be below failed count")
        if observations and self.failed == self.attempted:
            raise ValueError("ASR observations require a successful attempt")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "causes", causes)

    @property
    def status(self) -> BranchStatus:
        if self.branch_error is not None:
            return BranchStatus.FAILED
        if not self.causes:
            return BranchStatus.SUCCESS
        return BranchStatus.PARTIAL_FAILURE if self.observations else BranchStatus.FAILED

    @property
    def spans(self) -> tuple[TranscriptSpan, ...]:
        return self.observations

    @property
    def errors(self) -> tuple[Exception, ...]:
        return self.all_causes

    @property
    def all_causes(self) -> tuple[Exception, ...]:
        """Return branch-level and per-segment causes without changing counts."""

        if self.branch_error is None:
            return self.causes
        return (self.branch_error, *self.causes)


@dataclass(frozen=True, slots=True)
class OcrBranchOutcome:
    """Immutable OCR observations, duplicate skips, and per-frame failures."""

    observations: tuple[OcrObservation, ...] = ()
    attempted: int = 0
    failed: int = 0
    skipped_duplicates: int = 0
    causes: tuple[Exception, ...] = ()
    branch_error: Exception | None = None

    def __post_init__(self) -> None:
        observations = tuple(self.observations)
        causes = tuple(self.causes)
        if not all(isinstance(item, OcrObservation) for item in observations):
            raise TypeError("OCR observations must be OcrObservation values")
        if not all(isinstance(item, Exception) for item in causes):
            raise TypeError("OCR causes must be exceptions")
        if self.branch_error is not None and not isinstance(self.branch_error, Exception):
            raise TypeError("OCR branch_error must be an Exception or None")
        for name in ("attempted", "failed", "skipped_duplicates"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"OCR {name} count must be an integer")
            if value < 0:
                raise ValueError(f"OCR {name} count cannot be negative")
        if self.failed != len(causes):
            raise ValueError("OCR failed count must match causes")
        if self.attempted < self.failed:
            raise ValueError("OCR attempted count cannot be below failed count")
        if self.attempted != self.failed + len(observations):
            raise ValueError("OCR attempted count must equal observations plus failures")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "causes", causes)

    @property
    def status(self) -> BranchStatus:
        if self.branch_error is not None:
            return BranchStatus.FAILED
        if not self.causes:
            return BranchStatus.SUCCESS
        return BranchStatus.PARTIAL_FAILURE if self.observations else BranchStatus.FAILED

    @property
    def errors(self) -> tuple[Exception, ...]:
        return self.all_causes

    @property
    def all_causes(self) -> tuple[Exception, ...]:
        """Return branch-level and per-frame causes without changing counts."""

        if self.branch_error is None:
            return self.causes
        return (self.branch_error, *self.causes)


@dataclass(frozen=True, slots=True)
class MediaObservationBundle:
    """The two branch outcomes returned before Phase 4 context merging."""

    asr: AsrBranchOutcome
    ocr: OcrBranchOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.asr, AsrBranchOutcome):
            raise TypeError("asr must be an AsrBranchOutcome")
        if not isinstance(self.ocr, OcrBranchOutcome):
            raise TypeError("ocr must be an OcrBranchOutcome")

    @property
    def both_failed(self) -> bool:
        return self.asr.status is BranchStatus.FAILED and self.ocr.status is BranchStatus.FAILED


@dataclass(frozen=True, slots=True)
class VectorHit:
    """Provider-neutral vector search result."""

    start_ms: int
    end_ms: int
    score: float
    source_revision: str = ""
    chunk_id: str = ""
    chunking_version: str = ""


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Opaque trace identity carried across asynchronous adapter calls."""

    trace_id: str
    task_key: TaskKey

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("trace_id is required")
        if not isinstance(self.task_key, TaskKey):
            raise TypeError("task_key must be a TaskKey")


class DispatchDisposition(str, Enum):
    """The Java ``AnalysisDispatchService.SubmissionResult`` values."""

    ACCEPTED = "ACCEPTED"
    # Legacy submission contract; current TaskDispatchService never emits it.
    RATE_LIMITED = "RATE_LIMITED"
    DUPLICATE = "DUPLICATE"
    FAILED = "FAILED"


DEFAULT_ANALYSIS_MODE: Final[AnalysisMode] = AnalysisMode.GENERAL

# Descriptive aliases for callers that use ``BranchOutcome`` terminology.
AsrBranchResult = AsrBranchOutcome
OcrBranchResult = OcrBranchOutcome


def _resolve_mode(value: AnalysisMode | str | None) -> AnalysisMode:
    if isinstance(value, AnalysisMode):
        return value
    if value is None:
        return AnalysisMode.GENERAL
    if isinstance(value, str):
        return AnalysisMode.from_nullable(value)
    raise TypeError("mode must be an AnalysisMode, string, or None")


__all__ = [
    "AnalysisRequest",
    "AsrBranchOutcome",
    "AsrBranchResult",
    "BranchStatus",
    "DEFAULT_ANALYSIS_MODE",
    "DispatchDisposition",
    "MediaRef",
    "MediaObservationBundle",
    "OcrObservation",
    "OcrBranchOutcome",
    "OcrBranchResult",
    "ReadableSource",
    "TaskKey",
    "TraceContext",
    "TranscriptSpan",
    "VectorHit",
]

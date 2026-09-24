"""Exceptions raised by local media preprocessing adapters."""

from __future__ import annotations

from typing import Final


class MediaInfrastructureError(RuntimeError):
    """Base error for failures at the local media tool boundary."""


class SubprocessError(MediaInfrastructureError):
    """Base class for a process that could not be completed successfully."""

    def __init__(
        self,
        message: str,
        *,
        command: tuple[str, ...],
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class SubprocessLaunchError(SubprocessError):
    """The executable could not be started."""


class SubprocessExecutionError(SubprocessError):
    """The executable returned a non-zero exit code."""


class SubprocessTimeoutError(SubprocessError):
    """The executable exceeded its configured timeout and was terminated."""

    def __init__(
        self,
        message: str,
        *,
        command: tuple[str, ...],
        timeout_seconds: float,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(
            message,
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=None,
        )
        self.timeout_seconds = timeout_seconds


class MediaProbeError(MediaInfrastructureError):
    """The duration probe returned missing or invalid media metadata."""


class MediaPreprocessingError(MediaInfrastructureError):
    """A preprocessing operation could not produce a valid artifact set."""


class AsrError(MediaInfrastructureError):
    """Base error for a single-segment or HTTP ASR adapter."""


class AsrAudioMissing(AsrError):
    """An expected generated audio segment is absent."""


class AsrRequestRejected(AsrError):
    """A non-retryable ASR HTTP 4xx response."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"ASR request rejected with HTTP {status_code}")
        self.status_code = status_code


class AsrResponseError(AsrError):
    """An HTTP success response did not contain usable text."""


class AsrTransientFailure(AsrError):
    """ASR transport/transient HTTP failure after retry exhaustion."""


class OcrError(MediaInfrastructureError):
    """Base error for frame OCR and image hashing."""


class OcrImageMissing(OcrError):
    """An expected generated image is absent."""


class ImageHashError(OcrError):
    """An image could not be decoded or hashed."""


class ImageHashUnavailable(ImageHashError):
    """The optional Pillow decoder is not installed."""


class AllAsrSegmentsFailed(MediaPreprocessingError):
    """Every candidate ASR segment failed; ``causes`` preserves each error."""

    def __init__(
        self,
        causes: tuple[Exception, ...] | list[Exception],
        *,
        attempted: int | None = None,
    ) -> None:
        normalized = tuple(causes)
        if not normalized:
            raise ValueError("at least one ASR cause is required")
        if not all(isinstance(cause, Exception) for cause in normalized):
            raise TypeError("ASR causes must be Exception values")
        if attempted is not None and (
            not isinstance(attempted, int) or isinstance(attempted, bool)
        ):
            raise TypeError("ASR attempted count must be an integer")
        if attempted is not None and attempted < 0:
            raise ValueError("ASR attempted count cannot be negative")
        if attempted is not None and attempted < len(normalized):
            raise ValueError("ASR attempted count cannot be below failed count")
        self.causes: tuple[Exception, ...] = normalized
        self.last_cause = normalized[-1]
        self.attempted = len(normalized) if attempted is None else attempted
        super().__init__(f"all ASR segments failed ({len(normalized)})")


class AllOcrFramesFailed(MediaPreprocessingError):
    """Every candidate OCR frame failed; ``causes`` preserves each error."""

    def __init__(
        self,
        causes: tuple[Exception, ...] | list[Exception],
        *,
        attempted: int | None = None,
        skipped_duplicates: int = 0,
    ) -> None:
        normalized = tuple(causes)
        if not normalized:
            raise ValueError("at least one OCR cause is required")
        if not all(isinstance(cause, Exception) for cause in normalized):
            raise TypeError("OCR causes must be Exception values")
        if attempted is not None and (
            not isinstance(attempted, int) or isinstance(attempted, bool)
        ):
            raise TypeError("OCR attempted count must be an integer")
        if attempted is not None and attempted < 0:
            raise ValueError("OCR attempted count cannot be negative")
        if attempted is not None and attempted < len(normalized):
            raise ValueError("OCR attempted count cannot be below failed count")
        if not isinstance(skipped_duplicates, int) or isinstance(skipped_duplicates, bool):
            raise TypeError("OCR duplicate count must be an integer")
        if skipped_duplicates < 0:
            raise ValueError("OCR duplicate count cannot be negative")
        self.causes: tuple[Exception, ...] = normalized
        self.last_cause = normalized[-1]
        self.attempted = len(normalized) if attempted is None else attempted
        self.skipped_duplicates = skipped_duplicates
        super().__init__(f"all OCR frames failed ({len(normalized)})")


class BothMediaBranchesFailed(MediaPreprocessingError):
    """Both branch outcomes failed, retaining both structured causes."""

    def __init__(self, asr: object, ocr: object) -> None:
        self.asr = asr
        self.ocr = ocr
        asr_causes = _branch_causes(asr)
        ocr_causes = _branch_causes(ocr)
        causes = asr_causes + ocr_causes
        if not all(isinstance(cause, Exception) for cause in causes):
            raise TypeError("branch causes must be Exception values")
        self.causes: tuple[Exception, ...] = causes
        super().__init__("both ASR and OCR media branches failed")


class MediaBranchesTimeout(MediaPreprocessingError):
    """The concurrent ASR/OCR branches exceeded their shared time budget."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(f"media branches exceeded {timeout_seconds:g}s budget")
        self.timeout_seconds = timeout_seconds


class WorkspaceError(MediaInfrastructureError):
    """A temporary media workspace was used outside its lifetime."""


class WorkspaceClosedError(WorkspaceError):
    """An artifact path was requested after workspace cleanup."""


class WorkspaceCleanupError(WorkspaceError):
    """A workspace could not be removed completely."""


# Names used by a few callers during migration from Java terminology.
ProcessExecutionError: Final = SubprocessExecutionError
ProcessTimeoutError: Final = SubprocessTimeoutError
ProcessLaunchError: Final = SubprocessLaunchError


def _branch_causes(outcome: object) -> tuple[Exception, ...]:
    """Collect branch-level and item-level causes without widening types."""

    branch_error = getattr(outcome, "branch_error", None)
    causes = tuple(getattr(outcome, "causes", ()))
    if branch_error is None:
        return causes
    return (branch_error, *causes)


__all__ = [
    "AllAsrSegmentsFailed",
    "AllOcrFramesFailed",
    "AsrAudioMissing",
    "AsrError",
    "AsrRequestRejected",
    "AsrResponseError",
    "AsrTransientFailure",
    "BothMediaBranchesFailed",
    "MediaInfrastructureError",
    "MediaPreprocessingError",
    "MediaBranchesTimeout",
    "ImageHashError",
    "ImageHashUnavailable",
    "OcrError",
    "OcrImageMissing",
    "MediaProbeError",
    "ProcessExecutionError",
    "ProcessLaunchError",
    "ProcessTimeoutError",
    "SubprocessError",
    "SubprocessExecutionError",
    "SubprocessLaunchError",
    "SubprocessTimeoutError",
    "WorkspaceCleanupError",
    "WorkspaceClosedError",
    "WorkspaceError",
]

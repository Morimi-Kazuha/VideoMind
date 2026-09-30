"""Typed application errors for media ingest and resumable uploads."""

from __future__ import annotations


class MediaApplicationError(Exception):
    """Base class for expected media-boundary failures."""


class BudgetExceededError(RuntimeError):
    """The controlled Agent exceeded a configured token, cost, or deadline budget."""


class DeadlineExceededError(TimeoutError):
    """The current Agent execution deadline elapsed before a stage completed."""


class AgentFeedbackPersistenceError(RuntimeError):
    """The optional hot-only Agent feedback list could not be written/read."""


# Java calls this boundary exception ``BudgetExceededException``.  Keep both
# spellings available to adapters without creating two unrelated exception
# types.
BudgetExceededException = BudgetExceededError


class InvalidMediaInput(MediaApplicationError, ValueError):
    """The caller supplied malformed or unsupported media input."""


class UnsupportedVideoFormat(InvalidMediaInput):
    """The filename extension is outside the supported media formats."""


class MediaPayloadTooLarge(InvalidMediaInput):
    """A bounded upload exceeded its allowed payload size."""


class MediaUnauthorized(MediaApplicationError, PermissionError):
    """The caller does not own the media or upload session."""


class UploadNotFoundOrExpired(MediaApplicationError, LookupError):
    """An upload session/marker is absent or past its 24-hour TTL."""


class UploadConflict(MediaApplicationError):
    """A nonblocking merge lock or upload completeness check conflicted."""


class MediaStorageFailure(MediaApplicationError):
    """A storage operation failed; the original cause is chained by the adapter."""


class MediaRecordFailure(MediaApplicationError):
    """A media record could not be persisted after object upload."""


class VideoContextBuildError(MediaApplicationError):
    """Base class for deterministic Phase 4 context-build failures."""


class BothObservationBranchesFailed(VideoContextBuildError):
    """Both ASR and OCR branches failed before a context could be built."""

    def __init__(
        self,
        asr_causes: tuple[Exception, ...] | list[Exception],
        ocr_causes: tuple[Exception, ...] | list[Exception],
    ) -> None:
        normalized_asr = tuple(asr_causes)
        normalized_ocr = tuple(ocr_causes)
        if not all(
            isinstance(cause, Exception)
            for cause in (*normalized_asr, *normalized_ocr)
        ):
            raise TypeError("branch causes must be Exception values")
        self.asr_causes = normalized_asr
        self.ocr_causes = normalized_ocr
        self.causes = normalized_asr + normalized_ocr
        self.asr_error = normalized_asr[0] if normalized_asr else None
        self.ocr_error = normalized_ocr[0] if normalized_ocr else None
        super().__init__("ASR and OCR branches both failed")


class EmptyVideoContextError(VideoContextBuildError):
    """No merged window contains speech, OCR text, or a frame reference."""


# Descriptive aliases for callers migrating from Java branch terminology.
BothBranchesFailed = BothObservationBranchesFailed
BothMediaBranchesFailed = BothObservationBranchesFailed
NoUsableVideoEvidence = EmptyVideoContextError


class UrlValidationError(InvalidMediaInput):
    """The URL is not a permitted public HTTP/HTTPS source."""


class UrlResolutionError(InvalidMediaInput):
    """DNS resolution failed or produced no usable address."""


class UrlDownloadFailure(MediaApplicationError):
    """The bounded yt-dlp download failed or produced no file."""


# Compatibility aliases make the category names explicit to callers migrating
# from Java's IllegalArgumentException/SecurityException/BusinessException.
InvalidInputError = InvalidMediaInput
UnauthorizedError = MediaUnauthorized
ExpiredUploadError = UploadNotFoundOrExpired
UploadExpiredError = UploadNotFoundOrExpired
UploadNotFoundError = UploadNotFoundOrExpired
UploadConflictError = UploadConflict
InfrastructureFailureError = MediaStorageFailure


__all__ = [
    "BudgetExceededError",
    "BudgetExceededException",
    "DeadlineExceededError",
    "AgentFeedbackPersistenceError",
    "FeedbackPersistenceError",
    "ExpiredUploadError",
    "BothBranchesFailed",
    "BothMediaBranchesFailed",
    "BothObservationBranchesFailed",
    "EmptyVideoContextError",
    "InfrastructureFailureError",
    "InvalidInputError",
    "InvalidMediaInput",
    "MediaApplicationError",
    "MediaRecordFailure",
    "MediaStorageFailure",
    "MediaUnauthorized",
    "UnauthorizedError",
    "UploadConflict",
    "UploadConflictError",
    "UploadExpiredError",
    "UploadNotFoundError",
    "UploadNotFoundOrExpired",
    "UrlDownloadFailure",
    "UrlResolutionError",
    "UrlValidationError",
    "NoUsableVideoEvidence",
    "VideoContextBuildError",
]


FeedbackPersistenceError = AgentFeedbackPersistenceError

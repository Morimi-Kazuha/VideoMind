"""Application-level media ingest and resumable-upload contracts.

The values in this module are deliberately persistence/provider neutral.  A
record describes the business result of an ingest, while an upload session is
short-lived coordination state with an explicit 24-hour expiry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from uuid import UUID

from .errors import InvalidMediaInput

MEDIA_SESSION_TTL = timedelta(hours=24)
MAX_CHUNK_BYTES = 5 * 1024 * 1024
MAX_TOTAL_CHUNKS = 410
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"})


class MediaStatus(str, Enum):
    """Durable media-record state used by the Java ``MediaFile`` entity."""

    COMPLETED = "COMPLETED"


class UploadSessionState(str, Enum):
    """Lifecycle state for a resumable upload session."""

    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"


def normalize_video_filename(filename: str) -> str:
    """Apply the Java ``MediaService.normalizeVideoFilename`` rules.

    Backslashes are treated as path separators before the basename is taken,
    so a client cannot influence an object key with a path component.  The
    suffix check is case-insensitive, but the returned display name keeps the
    caller's spelling and case.
    """

    if not isinstance(filename, str) or not filename.strip():
        raise InvalidMediaInput("video filename is required")
    normalized = filename.replace("\\", "/")
    normalized = normalized.rsplit("/", 1)[-1].strip()
    if not normalized or len(normalized) > 255 or "\x00" in normalized:
        raise InvalidMediaInput("video filename is invalid or too long")
    suffix = _filename_suffix(normalized).casefold()
    if suffix not in VIDEO_SUFFIXES:
        raise InvalidMediaInput(
            "only MP4, MOV, MKV, AVI, WEBM, and M4V videos are supported"
        )
    return normalized


def filename_suffix(filename: str) -> str:
    """Return a validated, lower-case suffix suitable for an object key."""

    normalized = normalize_video_filename(filename)
    return _filename_suffix(normalized).casefold()


@dataclass(frozen=True, slots=True)
class MediaRecord:
    """Immutable durable media record returned after an ingest succeeds."""

    user_id: int
    filename: str
    source: str
    content_hash: str | None = None
    media_id: int | None = None
    status: MediaStatus = MediaStatus.COMPLETED
    uploaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content_type: str | None = None

    def __post_init__(self) -> None:
        _validate_user_id(self.user_id)
        object.__setattr__(self, "filename", normalize_video_filename(self.filename))
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("media source is required")
        if self.content_hash is not None:
            if not isinstance(self.content_hash, str) or not self.content_hash.strip():
                raise ValueError("content hash must be nonblank when provided")
            object.__setattr__(self, "content_hash", self.content_hash.strip().lower())
        if self.media_id is not None:
            if not isinstance(self.media_id, int) or isinstance(self.media_id, bool):
                raise TypeError("media_id must be an integer or None")
            if self.media_id <= 0:
                raise ValueError("media_id must be positive")
        object.__setattr__(self, "status", _resolve_status(self.status))
        if not isinstance(self.uploaded_at, datetime):
            raise TypeError("uploaded_at must be a datetime")
        object.__setattr__(self, "uploaded_at", _as_utc(self.uploaded_at))
        if self.content_type is not None and not isinstance(self.content_type, str):
            raise TypeError("content_type must be text or None")

    def to_ref(self):
        """Convert to the existing provider-neutral ``MediaRef`` value."""

        if self.media_id is None:
            raise ValueError("a persisted media record requires media_id")
        from .value_objects import MediaRef

        return MediaRef(
            media_id=self.media_id,
            source=self.source,
            filename=self.filename,
            content_hash=self.content_hash,
            status=self.status.value,
        )


@dataclass(frozen=True, slots=True)
class UploadSession:
    """24-hour resumable-upload metadata, independent of Redis/MinIO."""

    upload_id: str
    filename: str
    total_chunks: int
    user_id: int
    created_at: datetime
    expires_at: datetime
    state: UploadSessionState = UploadSessionState.ACTIVE

    def __post_init__(self) -> None:
        object.__setattr__(self, "upload_id", _canonical_upload_id(self.upload_id))
        object.__setattr__(self, "filename", normalize_video_filename(self.filename))
        if not isinstance(self.total_chunks, int) or isinstance(self.total_chunks, bool):
            raise TypeError("total_chunks must be an integer")
        if not 1 <= self.total_chunks <= MAX_TOTAL_CHUNKS:
            raise ValueError(f"total_chunks must be between 1 and {MAX_TOTAL_CHUNKS}")
        _validate_user_id(self.user_id)
        if not isinstance(self.created_at, datetime) or not isinstance(self.expires_at, datetime):
            raise TypeError("session timestamps must be datetimes")
        created_at = _as_utc(self.created_at)
        expires_at = _as_utc(self.expires_at)
        if expires_at <= created_at:
            raise ValueError("upload session must expire after creation")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "state", _resolve_session_state(self.state))

    def is_expired(self, now: datetime) -> bool:
        return _as_utc(now) >= self.expires_at

    def renewed(self, now: datetime, *, ttl: timedelta = MEDIA_SESSION_TTL) -> "UploadSession":
        if ttl <= timedelta(0):
            raise ValueError("upload session TTL must be positive")
        return UploadSession(
            upload_id=self.upload_id,
            filename=self.filename,
            total_chunks=self.total_chunks,
            user_id=self.user_id,
            created_at=self.created_at,
            expires_at=_as_utc(now) + ttl,
            state=self.state,
        )


@dataclass(frozen=True, slots=True)
class UploadStatus:
    """Owned upload status with numerically sorted, immutable chunk indexes."""

    session: UploadSession
    uploaded_chunks: tuple[int, ...] = ()
    completed_media_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session, UploadSession):
            raise TypeError("session must be an UploadSession")
        indexes = tuple(self.uploaded_chunks)
        if any(
            not isinstance(index, int) or isinstance(index, bool)
            or index < 0 or index >= self.session.total_chunks
            for index in indexes
        ):
            raise ValueError("uploaded chunk indexes are invalid")
        if indexes != tuple(sorted(set(indexes))):
            raise ValueError("uploaded chunk indexes must be unique and sorted")
        if self.completed_media_id is not None:
            if not isinstance(self.completed_media_id, int) or isinstance(self.completed_media_id, bool):
                raise TypeError("completed_media_id must be an integer or None")
            if self.completed_media_id <= 0:
                raise ValueError("completed_media_id must be positive")
        object.__setattr__(self, "uploaded_chunks", indexes)

    @property
    def state(self) -> UploadSessionState:
        return self.session.state


@dataclass(frozen=True, slots=True)
class CompletedUploadMarker:
    """Idempotency marker retained for 24 hours after a successful merge."""

    upload_id: str
    user_id: int
    media_id: int
    expires_at: datetime
    # These optional fields preserve enough session metadata for an idempotent
    # status response after active metadata and chunk keys are cleaned.  They
    # remain optional for compatibility with markers written by earlier code.
    filename: str | None = None
    total_chunks: int | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "upload_id", _canonical_upload_id(self.upload_id))
        _validate_user_id(self.user_id)
        if not isinstance(self.media_id, int) or isinstance(self.media_id, bool) or self.media_id <= 0:
            raise ValueError("media_id must be a positive integer")
        if not isinstance(self.expires_at, datetime):
            raise TypeError("expires_at must be a datetime")
        object.__setattr__(self, "expires_at", _as_utc(self.expires_at))
        if self.filename is not None:
            object.__setattr__(self, "filename", normalize_video_filename(self.filename))
        if self.total_chunks is not None:
            if not isinstance(self.total_chunks, int) or isinstance(self.total_chunks, bool):
                raise TypeError("total_chunks must be an integer or None")
            if not 1 <= self.total_chunks <= MAX_TOTAL_CHUNKS:
                raise ValueError(
                    f"total_chunks must be between 1 and {MAX_TOTAL_CHUNKS}"
                )
        if self.created_at is not None:
            if not isinstance(self.created_at, datetime):
                raise TypeError("created_at must be a datetime or None")
            object.__setattr__(self, "created_at", _as_utc(self.created_at))

    def is_expired(self, now: datetime) -> bool:
        return _as_utc(now) >= self.expires_at


@dataclass(frozen=True, slots=True)
class UrlDownloadResult:
    """A downloaded file valid only while its owning workspace is open."""

    path: Path
    filename: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "filename", normalize_video_filename(self.filename))


def _filename_suffix(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:] if dot >= 0 else ""


def _canonical_upload_id(upload_id: str) -> str:
    if not isinstance(upload_id, str):
        raise TypeError("upload_id must be text")
    try:
        return str(UUID(upload_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("upload_id must be a UUID") from exc


def _validate_user_id(user_id: int) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise TypeError("user_id must be an integer")
    if user_id < 0:
        raise ValueError("user_id cannot be negative")


def _resolve_status(status: MediaStatus | str) -> MediaStatus:
    if isinstance(status, MediaStatus):
        return status
    try:
        return MediaStatus(str(status).upper())
    except ValueError as exc:
        raise ValueError("unsupported media status") from exc


def _resolve_session_state(state: UploadSessionState | str) -> UploadSessionState:
    if isinstance(state, UploadSessionState):
        return state
    try:
        return UploadSessionState(str(state).upper())
    except ValueError as exc:
        raise ValueError("unsupported upload session state") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "CompletedUploadMarker",
    "MAX_CHUNK_BYTES",
    "MAX_TOTAL_CHUNKS",
    "MEDIA_SESSION_TTL",
    "MediaRecord",
    "MediaStatus",
    "UploadSession",
    "UploadSessionState",
    "UploadStatus",
    "UrlDownloadResult",
    "VIDEO_SUFFIXES",
    "filename_suffix",
    "normalize_video_filename",
]

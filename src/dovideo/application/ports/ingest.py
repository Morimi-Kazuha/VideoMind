"""Small ports used by the Phase 3C media-ingest boundary.

The interfaces describe only the calls made by the use cases.  They do not
expose Redis, MinIO, FastAPI, ORM entities, or a particular DNS/HTTP SDK.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Protocol

from ..media import (
    CompletedUploadMarker,
    MediaRecord,
    UploadSession,
    UrlDownloadResult,
)


class MediaRecordPort(Protocol):
    """Persist and retrieve immutable media records."""

    async def save(self, record: MediaRecord) -> MediaRecord:
        ...

    async def get(self, media_id: int) -> MediaRecord | None:
        ...

    async def get_by_source(self, source: str) -> MediaRecord | None:
        """Recover a committed upload result after receipt/response failure."""
        ...

    async def delete(self, media_id: int) -> None:
        """Delete a record during a failed merge rollback."""
        ...


MediaRepositoryPort = MediaRecordPort
MediaRecordWriterPort = MediaRecordPort


class ChunkObjectPort(Protocol):
    """Read/write/delete bounded chunk objects by safe object name."""

    async def put_chunk(
        self,
        object_name: str,
        chunks: AsyncIterable[bytes],
        *,
        size: int,
    ) -> None:
        ...

    def read_chunk(self, object_name: str) -> AsyncIterator[bytes]:
        ...

    async def delete_chunk(self, object_name: str) -> None:
        ...

    async def list_chunks(self, upload_id: str) -> tuple[int, ...]:
        ...


ChunkStorePort = ChunkObjectPort


class UploadSessionPort(Protocol):
    """Store active metadata and the short-lived completed marker."""

    async def create_session(self, session: UploadSession) -> None:
        ...

    async def get_session(self, upload_id: str) -> UploadSession | None:
        ...

    async def renew_session(self, upload_id: str, session: UploadSession) -> None:
        ...

    async def confirm_chunks(self, session: UploadSession, indexes: tuple[int, ...]) -> None:
        """Atomically confirm persisted objects and renew active state only."""
        ...

    async def get_uploaded_chunks(self, upload_id: str) -> tuple[int, ...] | None:
        """Confirmed indexes; None denotes pre-Set metadata needing migration."""
        ...

    async def delete_session(self, upload_id: str) -> None:
        ...

    async def get_completed(self, upload_id: str) -> CompletedUploadMarker | None:
        ...

    async def set_completed(self, marker: CompletedUploadMarker) -> None:
        ...

    async def delete_completed(self, upload_id: str) -> None:
        ...


UploadStorePort = UploadSessionPort


class MergeLockLease(Protocol):
    """A nonblocking per-upload lease released by the owning task."""

    def release(self) -> None:
        ...

    @property
    def ttl_seconds(self) -> float | None:
        """Finite Redis lease duration; local locks have no expiration."""
        ...

    def refresh(self) -> None:
        """Renew only the owning lease; raise if ownership has been lost."""
        ...


class MergeLockPort(Protocol):
    """Attempt to acquire a merge lock without waiting."""

    def try_acquire(self, upload_id: str) -> MergeLockLease | None:
        ...


NonBlockingMergeLockPort = MergeLockPort


class UrlDownloadPort(Protocol):
    """Download one validated URL into a caller-owned active workspace."""

    async def download(self, url: str, workspace: object) -> UrlDownloadResult:
        """Return a result whose path is valid only during ``workspace``."""
        ...


UrlDownloaderPort = UrlDownloadPort


class DnsResolverPort(Protocol):
    """Resolve a hostname asynchronously to textual IP addresses."""

    async def resolve(self, host: str) -> tuple[str, ...]:
        ...


__all__ = [
    "ChunkObjectPort",
    "ChunkStorePort",
    "DnsResolverPort",
    "MediaRecordPort",
    "MediaRecordWriterPort",
    "MediaRepositoryPort",
    "MergeLockLease",
    "MergeLockPort",
    "NonBlockingMergeLockPort",
    "UploadSessionPort",
    "UploadStorePort",
    "UrlDownloadPort",
    "UrlDownloaderPort",
]

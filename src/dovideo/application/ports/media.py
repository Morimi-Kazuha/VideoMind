"""Media metadata, object storage, and readable-source boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterable
from pathlib import Path
from typing import Protocol

from ..value_objects import MediaRef, ReadableSource


class MediaMetadataPort(Protocol):
    """Read media metadata without exposing a persistence model."""

    async def get_media(self, media_id: int) -> MediaRef | None:
        """Return metadata, or ``None`` when the media record is absent."""


class ObjectStoragePort(Protocol):
    """Store/delete media objects; key and URL semantics stay provider-neutral."""

    async def put_object(
        self,
        chunks: AsyncIterable[bytes],
        *,
        object_name: str,
        content_type: str | None = None,
    ) -> str:
        """Persist a stream and return its opaque source/URL."""

    async def delete_object(self, source: str) -> None:
        """Delete an object previously returned by :meth:`put_object`."""


class ReadableSourcePort(Protocol):
    """Resolve an object/source token to an ASR/OCR-readable token."""

    async def resolve_readable_source(self, source: str) -> ReadableSource:
        ...


class EvidenceFramePort(Protocol):
    """Persist one OCR evidence frame and return a durable reference."""

    async def persist_frame(self, image_path: Path, *, timestamp_ms: int) -> str:
        ...


FramePersistencePort = EvidenceFramePort


class ImageHashPort(Protocol):
    """Pure/synchronous perceptual hash calculation for one frame."""

    def difference_hash(self, image_path: Path) -> int:
        ...


# Short aliases keep adapter naming ergonomic without combining capabilities.
MediaObjectPort = ObjectStoragePort
MediaSourcePort = ReadableSourcePort


__all__ = [
    "MediaMetadataPort",
    "MediaObjectPort",
    "MediaSourcePort",
    "ObjectStoragePort",
    "ReadableSourcePort",
    "EvidenceFramePort",
    "FramePersistencePort",
    "ImageHashPort",
]

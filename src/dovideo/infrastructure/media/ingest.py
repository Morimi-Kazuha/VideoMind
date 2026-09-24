"""Direct-file and URL media ingestion for Phase 3C.

The service streams caller-owned input through an object-storage port, computes
the Java-compatible MD5 content identifier in the same pass, and writes the
media record only after the object upload succeeds.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import os
from collections.abc import AsyncIterable
from pathlib import Path
from os import PathLike
from typing import BinaryIO
from uuid import uuid4

from dovideo.application import (
    InvalidMediaInput,
    MediaRecord,
    MediaRecordFailure,
    MediaStorageFailure,
    MediaUnauthorized,
    UrlDownloadFailure,
    filename_suffix,
    normalize_video_filename,
)
from dovideo.application.ports.ingest import MediaRecordPort, UrlDownloadPort
from dovideo.application.ports.media import ObjectStoragePort
from dovideo.application.ports.observability import ClockPort

from .memory import SystemClock
from .workspace import MediaWorkspace

STREAM_CHUNK_BYTES = 64 * 1024
MD5_READ_BYTES = 8 * 1024
MEDIA_OBJECT_PREFIX = "media/"


class MediaIngestService:
    """Store direct files or validated URL downloads without provider SDKs."""

    def __init__(
        self,
        object_storage: ObjectStoragePort,
        records: MediaRecordPort,
        downloader: UrlDownloadPort | None = None,
        *,
        clock: ClockPort | None = None,
        workspace_parent: str | PathLike[str] | None = None,
        stream_chunk_bytes: int = STREAM_CHUNK_BYTES,
    ) -> None:
        if stream_chunk_bytes <= 0:
            raise ValueError("stream_chunk_bytes must be positive")
        self._object_storage = object_storage
        self._records = records
        self._downloader = downloader
        self._clock = clock or SystemClock()
        self._workspace_parent = workspace_parent
        self._stream_chunk_bytes = stream_chunk_bytes

    async def ingest_file(
        self,
        file: BinaryIO | bytes | bytearray | PathLike[str],
        user_id: int,
        *,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> MediaRecord:
        """Stream a local upload, rejecting empty input before object storage."""

        _require_user(user_id)
        resolved_filename = (
            filename if filename is not None else _filename_from_input(file)
        )
        normalized = normalize_video_filename(resolved_filename)
        stream, should_close = _open_input(file)
        try:
            return await self._store_stream(
                stream,
                normalized,
                user_id,
                content_type=content_type,
            )
        finally:
            if should_close:
                await asyncio.to_thread(stream.close)

    async def ingest_url(self, url: str, user_id: int) -> MediaRecord:
        """Download into a scoped workspace, then stream it into storage."""

        _require_user(user_id)
        if self._downloader is None:
            raise MediaStorageFailure("URL downloader is not configured")
        async with MediaWorkspace(parent=self._workspace_parent) as workspace:
            downloaded = await self._downloader.download(url, workspace)
            path = Path(downloaded.path)
            try:
                path = path.resolve(strict=True)
                path.relative_to(workspace.path)
            except (OSError, ValueError) as exc:
                raise UrlDownloadFailure(
                    "URL downloader returned a path outside its workspace"
                ) from exc
            if not path.is_file() or path.stat().st_size <= 0:
                raise UrlDownloadFailure("URL downloader returned no non-empty file")
            normalized = normalize_video_filename(downloaded.filename)
            stream = path.open("rb")
            try:
                return await self._store_stream(
                    stream,
                    normalized,
                    user_id,
                    content_type=None,
                )
            finally:
                await asyncio.to_thread(stream.close)

    ingest = ingest_file

    async def calculate_md5(self, source: BinaryIO | PathLike[str]) -> str:
        """Calculate Java-compatible MD5 in bounded 8 KiB reads.

        MD5 is retained solely as a content-compatibility identifier; it is
        not a security hash and must not be used for authenticity decisions.
        """

        stream, should_close = _open_input(source)
        digest = hashlib.md5()
        try:
            while True:
                chunk = await asyncio.to_thread(stream.read, MD5_READ_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("media input must yield bytes")
                digest.update(bytes(chunk))
        finally:
            if should_close:
                await asyncio.to_thread(stream.close)
        return digest.hexdigest()

    async def _store_stream(
        self,
        stream: BinaryIO,
        filename: str,
        user_id: int,
        *,
        content_type: str | None,
    ) -> MediaRecord:
        normalized = normalize_video_filename(filename)
        digest = hashlib.md5()
        first = await _read_bytes(stream, self._stream_chunk_bytes)
        if not first:
            raise InvalidMediaInput("uploaded media cannot be empty")

        async def chunks() -> AsyncIterable[bytes]:
            digest.update(first)
            yield first
            while True:
                chunk = await _read_bytes(stream, self._stream_chunk_bytes)
                if not chunk:
                    return
                digest.update(chunk)
                yield chunk

        object_name = f"{MEDIA_OBJECT_PREFIX}{uuid4().hex}{filename_suffix(normalized)}"
        try:
            source = await self._object_storage.put_object(
                chunks(),
                object_name=object_name,
                content_type=content_type or _content_type(normalized),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MediaStorageFailure("media object upload failed") from exc
        if not isinstance(source, str) or not source.strip():
            raise MediaStorageFailure("media object storage returned an empty source")

        record = MediaRecord(
            user_id=user_id,
            filename=normalized,
            source=source,
            content_hash=digest.hexdigest(),
            uploaded_at=self._clock.now(),
            content_type=content_type or _content_type(normalized),
        )
        try:
            saved = await self._records.save(record)
            if not isinstance(saved, MediaRecord) or saved.media_id is None:
                raise MediaRecordFailure(
                    "media record port returned no persisted media id"
                )
        except asyncio.CancelledError as exc:
            await _rollback_object(self._object_storage, source, exc)
            raise
        except Exception as exc:
            await _rollback_object(self._object_storage, source, exc)
            raise
        return saved


async def _rollback_object(
    storage: ObjectStoragePort,
    source: str,
    original: BaseException,
) -> None:
    try:
        await storage.delete_object(source)
    except Exception as cleanup_error:
        original.add_note(f"media object rollback failed: {cleanup_error!r}")
        setattr(original, "cleanup_error", cleanup_error)


async def _read_bytes(stream: BinaryIO, size: int) -> bytes:
    chunk = await asyncio.to_thread(stream.read, size)
    if chunk is None:
        return b""
    if not isinstance(chunk, (bytes, bytearray, memoryview)):
        raise TypeError("media input must yield bytes")
    return bytes(chunk)


def _open_input(
    source: BinaryIO | bytes | bytearray | PathLike[str],
) -> tuple[BinaryIO, bool]:
    if isinstance(source, (bytes, bytearray)):
        return io.BytesIO(bytes(source)), True
    if isinstance(source, (str, os.PathLike)):
        return Path(os.fspath(source)).open("rb"), True
    if not hasattr(source, "read"):
        raise TypeError("media input must be bytes, a path, or a binary stream")
    return source, False  # type: ignore[return-value]


def _filename_from_input(source: object) -> str:
    if isinstance(source, (str, os.PathLike)):
        return Path(os.fspath(source)).name
    for attribute in ("filename", "name"):
        value = getattr(source, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    raise InvalidMediaInput("video filename is required")


def _content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _require_user(user_id: int) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 0:
        raise MediaUnauthorized("authenticated user is required")


__all__ = [
    "MD5_READ_BYTES",
    "MEDIA_OBJECT_PREFIX",
    "MediaIngestService",
    "STREAM_CHUNK_BYTES",
    "normalize_video_filename",
]

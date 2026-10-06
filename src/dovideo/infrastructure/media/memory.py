"""In-memory 3C adapters for tests and local development only.

These classes deliberately model the application ports without pretending to
be Redis, MinIO, or a production database.  They provide deterministic TTL,
failure injection, and concurrency behavior for offline tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from dovideo.application import (
    CompletedUploadMarker,
    MediaRecord,
    UploadSession,
)
from dovideo.application.ports.ingest import (
    ChunkObjectPort,
    MediaRecordPort,
    MergeLockLease,
    MergeLockPort,
    UploadSessionPort,
)
from dovideo.application.ports.media import ObjectStoragePort
from dovideo.application.ports.observability import ClockPort

from .errors import MediaInfrastructureError

class MutableClock:
    """Deterministic wall/monotonic clock used by TTL tests."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = _as_utc(now or datetime.now(timezone.utc))
        self._monotonic = self._now.timestamp()

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("clock cannot move backwards")
        self._now += delta
        self._monotonic += delta.total_seconds()


class SystemClock:
    """Small standard-library clock implementation for local use."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class InMemoryObjectStorage(ObjectStoragePort):
    """Bounded-test object storage with opaque ``memory://`` sources."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.put_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def put_object(
        self,
        chunks: AsyncIterable[bytes],
        *,
        object_name: str,
        content_type: str | None = None,
    ) -> str:
        del content_type
        _validate_object_name(object_name)
        if self.put_error is not None:
            raise self.put_error
        payload = bytearray()
        async for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("object storage chunks must be bytes")
            payload.extend(chunk)
        self.objects[object_name] = bytes(payload)
        self.put_calls.append(object_name)
        return self.source_for(object_name)

    async def delete_object(self, source: str) -> None:
        object_name = self.object_name_from_source(source)
        if self.delete_error is not None:
            raise self.delete_error
        self.objects.pop(object_name, None)
        self.delete_calls.append(object_name)

    async def read_object(self, object_name: str) -> bytes:
        _validate_object_name(object_name)
        try:
            return self.objects[object_name]
        except KeyError as exc:
            raise MediaInfrastructureError("object does not exist") from exc

    def source_for(self, object_name: str) -> str:
        _validate_object_name(object_name)
        return f"memory://{object_name}"

    def object_name_from_source(self, source: str) -> str:
        if not isinstance(source, str):
            raise ValueError("unknown in-memory object source")
        parsed = urlsplit(source)
        if parsed.scheme != "memory" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("unknown in-memory object source")
        # ``memory://media/foo`` parses ``media`` as netloc and ``/foo`` as
        # path.  Rejoin both pieces so opaque object keys round-trip exactly.
        object_name = f"{parsed.netloc}{parsed.path}"
        _validate_object_name(object_name)
        return object_name


class InMemoryMediaRecordStore(MediaRecordPort):
    """Auto-ID media-record store for offline use-case tests."""

    def __init__(self) -> None:
        self.records: dict[int, MediaRecord] = {}
        self.next_id = 1
        self.save_calls = 0
        self.delete_calls: list[int] = []
        self.save_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def save(self, record: MediaRecord) -> MediaRecord:
        if self.save_error is not None:
            raise self.save_error
        self.save_calls += 1
        if record.media_id is None:
            record = replace(record, media_id=self.next_id)
            self.next_id += 1
        self.records[record.media_id] = record
        return record

    async def get(self, media_id: int) -> MediaRecord | None:
        return self.records.get(media_id)

    async def get_by_source(self, source: str) -> MediaRecord | None:
        return next((record for record in self.records.values() if record.source == source), None)

    async def delete(self, media_id: int) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.delete_calls.append(media_id)
        self.records.pop(media_id, None)


class InMemoryChunkObjectStore(ChunkObjectPort):
    """Chunk object store that yields data in bounded pieces."""

    def __init__(self, *, read_piece_bytes: int = 64 * 1024) -> None:
        if read_piece_bytes <= 0:
            raise ValueError("read_piece_bytes must be positive")
        self.objects: dict[str, bytes] = {}
        self.read_piece_bytes = read_piece_bytes
        self.put_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.put_error: Exception | None = None
        self.read_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def put_chunk(
        self,
        object_name: str,
        chunks: AsyncIterable[bytes],
        *,
        size: int,
    ) -> None:
        _validate_object_name(object_name)
        if self.put_error is not None:
            raise self.put_error
        payload = bytearray()
        async for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("chunk store values must be bytes")
            payload.extend(chunk)
        if len(payload) != size:
            raise ValueError("chunk stream size does not match declared size")
        self.objects[object_name] = bytes(payload)
        self.put_calls.append(object_name)

    def read_chunk(self, object_name: str) -> AsyncIterator[bytes]:
        _validate_object_name(object_name)

        async def iterator() -> AsyncIterator[bytes]:
            if self.read_error is not None:
                raise self.read_error
            try:
                payload = self.objects[object_name]
            except KeyError as exc:
                raise MediaInfrastructureError("chunk object does not exist") from exc
            for offset in range(0, len(payload), self.read_piece_bytes):
                yield payload[offset : offset + self.read_piece_bytes]

        return iterator()

    async def delete_chunk(self, object_name: str) -> None:
        _validate_object_name(object_name)
        if self.delete_error is not None:
            raise self.delete_error
        self.objects.pop(object_name, None)
        self.delete_calls.append(object_name)

    async def list_chunks(self, upload_id: str) -> tuple[int, ...]:
        indexes: list[int] = []
        prefix = f"chunk-uploads/{upload_id}/part-"
        for object_name in self.objects:
            if object_name.startswith(prefix):
                suffix = object_name[len(prefix) :]
                if suffix.isdigit():
                    indexes.append(int(suffix))
        return tuple(sorted(set(indexes)))


class InMemoryUploadSessionStore(UploadSessionPort):
    """TTL-aware active-session/marker store for tests and local development."""

    def __init__(self, clock: ClockPort | None = None) -> None:
        self.clock = clock or SystemClock()
        self.sessions: dict[str, UploadSession] = {}
        self.completed: dict[str, CompletedUploadMarker] = {}
        self.parts: dict[str, set[int]] = {}
        self.create_calls = 0
        self.renew_calls = 0
        self.delete_session_calls: list[str] = []
        self.delete_completed_calls: list[str] = []
        self.set_completed_calls = 0
        self.delete_session_error: Exception | None = None
        self.delete_completed_error: Exception | None = None
        self.set_completed_error: Exception | None = None

    async def create_session(self, session: UploadSession) -> None:
        self._purge_expired()
        self.sessions[session.upload_id] = session
        self.parts[session.upload_id] = set()
        self.create_calls += 1

    async def get_session(self, upload_id: str) -> UploadSession | None:
        self._purge_expired()
        return self.sessions.get(upload_id)

    async def renew_session(self, upload_id: str, session: UploadSession) -> None:
        self._purge_expired()
        if upload_id not in self.sessions:
            raise KeyError(upload_id)
        self.sessions[upload_id] = session
        self.renew_calls += 1

    async def confirm_chunks(self, session: UploadSession, indexes: tuple[int, ...]) -> None:
        from dovideo.application import UploadConflict, UploadNotFoundOrExpired

        self._purge_expired()
        if await self.get_completed(session.upload_id) is not None:
            raise UploadConflict("upload has already completed")
        current = self.sessions.get(session.upload_id)
        if current is None:
            raise UploadNotFoundOrExpired("upload does not exist or has expired")
        if current.user_id != session.user_id or current.total_chunks != session.total_chunks:
            raise UploadConflict("upload session changed")
        if any(not 0 <= index < session.total_chunks for index in indexes):
            raise UploadConflict("upload chunk state is invalid")
        self.parts.setdefault(session.upload_id, set()).update(indexes)
        await self.renew_session(session.upload_id, session)

    async def get_uploaded_chunks(self, upload_id: str) -> tuple[int, ...] | None:
        self._purge_expired()
        return tuple(sorted(self.parts.get(upload_id, set())))

    async def delete_session(self, upload_id: str) -> None:
        if self.delete_session_error is not None:
            raise self.delete_session_error
        self.sessions.pop(upload_id, None)
        self.parts.pop(upload_id, None)
        self.delete_session_calls.append(upload_id)

    async def get_completed(self, upload_id: str) -> CompletedUploadMarker | None:
        marker = self.completed.get(upload_id)
        if marker is not None and marker.is_expired(self.clock.now()):
            self.completed.pop(upload_id, None)
            return None
        return marker

    async def set_completed(self, marker: CompletedUploadMarker) -> None:
        if self.set_completed_error is not None:
            raise self.set_completed_error
        self.completed[marker.upload_id] = marker
        self.set_completed_calls += 1

    async def delete_completed(self, upload_id: str) -> None:
        if self.delete_completed_error is not None:
            raise self.delete_completed_error
        self.completed.pop(upload_id, None)
        self.delete_completed_calls.append(upload_id)

    def _purge_expired(self) -> None:
        now = self.clock.now()
        for upload_id, session in tuple(self.sessions.items()):
            if session.is_expired(now):
                del self.sessions[upload_id]
                self.parts.pop(upload_id, None)


class _InMemoryMergeLease(MergeLockLease):
    ttl_seconds = None

    def refresh(self) -> None:
        if self._released:
            raise RuntimeError("merge lease has been released")

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


class InMemoryMergeLock(MergeLockPort):
    """Nonblocking per-upload lock; useful for concurrent-complete tests."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def try_acquire(self, upload_id: str) -> MergeLockLease | None:
        with self._guard:
            lock = self._locks.setdefault(upload_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return None
        return _InMemoryMergeLease(lock)


# Descriptive aliases for callers using either naming convention.
MemoryObjectStorage = InMemoryObjectStorage
MemoryMediaRecordStore = InMemoryMediaRecordStore
MemoryChunkStore = InMemoryChunkObjectStore
MemoryUploadStore = InMemoryUploadSessionStore
MemoryMergeLock = InMemoryMergeLock


def _validate_object_name(object_name: str) -> None:
    if (
        not isinstance(object_name, str)
        or not object_name
        or "\x00" in object_name
        or "\\" in object_name
        or ".." in object_name
        or object_name.startswith("/")
        or any(part in ("", ".", "..") for part in object_name.split("/"))
    ):
        raise ValueError("object name is invalid")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "InMemoryChunkObjectStore",
    "InMemoryMediaRecordStore",
    "InMemoryMergeLock",
    "InMemoryObjectStorage",
    "InMemoryUploadSessionStore",
    "MemoryChunkStore",
    "MemoryMediaRecordStore",
    "MemoryMergeLock",
    "MemoryObjectStorage",
    "MemoryUploadStore",
    "MutableClock",
    "SystemClock",
]

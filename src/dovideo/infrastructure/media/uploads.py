"""Resumable media upload orchestration for Phase 3C."""

from __future__ import annotations

import asyncio
from dovideo.application.errors import MediaPayloadTooLarge
import hashlib
import mimetypes
import os
from collections.abc import AsyncIterable
from datetime import timedelta
from os import PathLike
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from dovideo.application import (
    CompletedUploadMarker,
    InvalidMediaInput,
    MAX_CHUNK_BYTES,
    MAX_TOTAL_CHUNKS,
    MEDIA_SESSION_TTL,
    MediaRecord,
    MediaStorageFailure,
    MediaUnauthorized,
    MediaRecordFailure,
    UploadConflict,
    UploadNotFoundOrExpired,
    UploadSession,
    UploadSessionState,
    UploadStatus,
    filename_suffix,
    normalize_video_filename,
)
from dovideo.application.ports.ingest import (
    ChunkObjectPort,
    MediaRecordPort,
    MergeLockPort,
    MergeLockLease,
    UploadSessionPort,
)
from dovideo.application.ports.media import ObjectStoragePort
from dovideo.application.ports.observability import ClockPort

from .ingest import MEDIA_OBJECT_PREFIX, STREAM_CHUNK_BYTES
from .memory import SystemClock
from .workspace import MediaWorkspace

CHUNK_OBJECT_PREFIX = "chunk-uploads/"
CHUNK_CONTENT_TYPE = "application/octet-stream"
MERGE_TIMEOUT_SECONDS = 20 * 60
CLEANUP_TIMEOUT_SECONDS = 10


class ChunkUploadService:
    """Coordinate bounded chunks, exact ordered merge, and 24-hour idempotency."""

    def __init__(
        self,
        chunks: ChunkObjectPort,
        object_storage: ObjectStoragePort,
        records: MediaRecordPort,
        sessions: UploadSessionPort,
        merge_lock: MergeLockPort,
        *,
        clock: ClockPort | None = None,
        workspace_parent: str | PathLike[str] | None = None,
        ttl: timedelta = MEDIA_SESSION_TTL,
        max_chunk_bytes: int = MAX_CHUNK_BYTES,
        max_total_chunks: int = MAX_TOTAL_CHUNKS,
        merge_timeout_seconds: float = MERGE_TIMEOUT_SECONDS,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("upload TTL must be positive")
        if not isinstance(max_chunk_bytes, int) or isinstance(max_chunk_bytes, bool) or max_chunk_bytes <= 0:
            raise ValueError("max_chunk_bytes must be positive")
        if max_chunk_bytes > MAX_CHUNK_BYTES:
            raise ValueError(f"max_chunk_bytes cannot exceed {MAX_CHUNK_BYTES}")
        if (
            not isinstance(max_total_chunks, int)
            or isinstance(max_total_chunks, bool)
            or not 1 <= max_total_chunks <= MAX_TOTAL_CHUNKS
        ):
            raise ValueError(f"max_total_chunks must be between 1 and {MAX_TOTAL_CHUNKS}")
        self._chunks = chunks
        self._object_storage = object_storage
        self._records = records
        self._sessions = sessions
        self._merge_lock = merge_lock
        self._clock = clock or SystemClock()
        self._workspace_parent = workspace_parent
        self._ttl = ttl
        self._max_chunk_bytes = max_chunk_bytes
        self._max_total_chunks = max_total_chunks
        if merge_timeout_seconds <= 0:
            raise ValueError("merge timeout must be positive")
        self._merge_timeout_seconds = merge_timeout_seconds

    async def initialize(
        self,
        filename: str,
        total_chunks: int,
        user_id: int,
    ) -> str:
        """Create a Java-compatible UUID upload id and 24-hour active session."""

        return (await self.initialize_session(filename, total_chunks, user_id)).upload_id

    async def initialize_session(
        self,
        filename: str,
        total_chunks: int,
        user_id: int,
    ) -> UploadSession:
        _require_user(user_id)
        normalized = normalize_video_filename(filename)
        _validate_total_chunks(total_chunks, self._max_total_chunks)
        now = self._clock.now()
        session = UploadSession(
            upload_id=str(uuid4()),
            filename=normalized,
            total_chunks=total_chunks,
            user_id=user_id,
            created_at=now,
            expires_at=now + self._ttl,
        )
        await self._sessions.create_session(session)
        return session

    async def status(self, upload_id: str, user_id: int) -> UploadStatus:
        _require_user(user_id)
        canonical = _canonical_upload_id(upload_id)
        marker = await self._valid_completed_marker(canonical, user_id)
        if marker is not None:
            record = await self._records.get(marker.media_id)
            if record is None:
                await self._sessions.delete_completed(canonical)
            else:
                if record.user_id != user_id:
                    raise MediaUnauthorized("completed media is owned by another user")
                if marker.filename is None or marker.total_chunks is None or marker.created_at is None:
                    # Preserve legacy receipts: deleting one destroys business
                    # idempotency. Recover shape from surviving metadata only.
                    original = await self._sessions.get_session(canonical)
                    if original is None:
                        raise UploadConflict("completed status metadata unavailable; retry complete")
                    if original.user_id != user_id:
                        raise MediaUnauthorized("upload is owned by another user")
                    filename, total_chunks, created_at = original.filename, original.total_chunks, original.created_at
                else:
                    filename, total_chunks, created_at = marker.filename, marker.total_chunks, marker.created_at
                # A completed session's active metadata may have been
                # cleaned; the marker carries the original shape when it
                # was written by this implementation.
                session = UploadSession(
                    upload_id=canonical,
                    filename=filename,
                    total_chunks=total_chunks,
                    user_id=user_id,
                    created_at=created_at,
                    expires_at=marker.expires_at,
                    state=UploadSessionState.COMPLETED,
                )
                return UploadStatus(
                    session=session,
                    uploaded_chunks=(),
                    completed_media_id=record.media_id,
                )
        session = await self._require_active(canonical, user_id)
        indexes = await self._sorted_chunk_indexes(session)
        return UploadStatus(session=session, uploaded_chunks=indexes)

    async def uploaded_chunks(self, upload_id: str, user_id: int) -> tuple[int, ...]:
        return (await self.status(upload_id, user_id)).uploaded_chunks

    async def upload_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        total_chunks: int,
        chunk: BinaryIO | bytes | bytearray | PathLike[str],
        user_id: int,
    ) -> None:
        """Validate and overwrite one bounded chunk idempotently."""

        _require_user(user_id)
        canonical = _canonical_upload_id(upload_id)
        session = await self._require_active(canonical, user_id)
        _validate_total_chunks(total_chunks, self._max_total_chunks)
        if total_chunks != session.total_chunks:
            raise InvalidMediaInput("total_chunks does not match the upload session")
        if not isinstance(chunk_index, int) or isinstance(chunk_index, bool):
            raise InvalidMediaInput("chunk_index must be an integer")
        if not 0 <= chunk_index < session.total_chunks:
            raise InvalidMediaInput("chunk_index is outside the upload session")
        payload = await _read_limited(chunk, self._max_chunk_bytes)
        if not payload:
            raise InvalidMediaInput("chunk cannot be empty")
        await self._sorted_chunk_indexes(session)  # migrate legacy state before new confirmation
        object_name = _chunk_object_name(canonical, chunk_index)

        async def one_chunk() -> AsyncIterable[bytes]:
            yield payload

        try:
            await self._chunks.put_chunk(object_name, one_chunk(), size=len(payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MediaStorageFailure("chunk object upload failed") from exc
        renewed = session.renewed(self._clock.now(), ttl=self._ttl)
        try:
            await self._sessions.confirm_chunks(renewed, (chunk_index,))
        except asyncio.CancelledError:
            raise
        except (UploadConflict, UploadNotFoundOrExpired):
            raise
        except Exception as exc:
            # The write may have succeeded or a duplicate may already be
            # confirmed. Never delete valid data after an uncertain Redis ACK.
            raise MediaStorageFailure("upload chunk confirmation failed") from exc

    async def complete(self, upload_id: str, user_id: int) -> MediaRecord:
        """Merge exact indexes once, with a nonblocking per-upload lock."""

        _require_user(user_id)
        canonical = _canonical_upload_id(upload_id)
        # Check ownership before attempting the nonblocking lock.  An
        # unrelated caller must not be able to turn a held lock into a
        # misleading conflict response (and must never learn merge timing).
        await self._verify_owner(canonical, user_id)
        try:
            lease = await asyncio.to_thread(self._merge_lock.try_acquire, canonical)
        except Exception as exc:
            raise MediaStorageFailure("upload merge lock unavailable") from exc
        if lease is None:
            raise UploadConflict("upload is already being merged")
        try:
            if lease.ttl_seconds is not None and self._merge_timeout_seconds >= lease.ttl_seconds:
                raise MediaStorageFailure("merge deadline must be shorter than lock lease")
            async with asyncio.timeout(self._merge_timeout_seconds):
                result, cleanup_chunks = await self._complete_locked(canonical, user_id, lease)
        except TimeoutError as exc:
            raise MediaStorageFailure("upload merge deadline exceeded; retry complete") from exc
        finally:
            await asyncio.to_thread(lease.release)
        # Core completion has succeeded. Cleanup has its own small deadline,
        # outside the merge deadline, and cannot turn success into failure.
        if cleanup_chunks:
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await self._best_effort_cleanup(canonical, cleanup_chunks)
            except Exception:
                pass
        return result

    async def _complete_locked(
        self, canonical: str, user_id: int, lease: MergeLockLease,
    ) -> tuple[MediaRecord, int]:
        try:
            completed = await self._completed_record(canonical, user_id)
            if completed is not None:
                return completed, 0
            session = await self._require_active(canonical, user_id)
            object_name = _final_object_name(session)
            source = self._object_storage.source_for(object_name)
            committed = await self._records.get_by_source(source)
            if committed is not None:
                if committed.user_id != user_id:
                    raise MediaUnauthorized("completed media is owned by another user")
                # MySQL committed but receipt creation/response failed. Rebuild
                # the receipt without a second merge or a second MediaRecord.
                return await self._finish_completion(session, committed, lease)
            indexes = await self._sorted_chunk_indexes(session)
            expected = tuple(range(session.total_chunks))
            if indexes != expected:
                raise UploadConflict(
                    f"upload chunks are incomplete ({len(indexes)}/{session.total_chunks})"
                )
            session = session.renewed(self._clock.now(), ttl=self._ttl)
            await self._sessions.renew_session(canonical, session)

            workspace = MediaWorkspace(parent=self._workspace_parent, prefix="dovideo-merge-")
            result: tuple[MediaRecord, int] | None = None
            merge_error: BaseException | None = None
            entered = False
            try:
                await workspace.__aenter__()
                entered = True
                result = await self._merge_in_workspace(
                    session,
                    canonical,
                    user_id,
                    workspace,
                    lease,
                )
            except BaseException as exc:
                merge_error = exc
            finally:
                if entered:
                    try:
                        await workspace.close()
                    except Exception as cleanup_error:
                        if merge_error is None:
                            if result is None:
                                merge_error = cleanup_error
                            else:
                                # The merge marker and record are already
                                # durable; cleanup failure must not turn a
                                # successful completion into a retryable one.
                                pass
                        else:
                            merge_error.add_note(
                                f"merge workspace cleanup failed: {cleanup_error!r}"
                            )
            if merge_error is not None:
                raise merge_error
            if result is None:  # defensive; _merge_in_workspace always returns
                raise MediaStorageFailure("merge did not produce a media record")
            return result
        except (MediaUnauthorized, UploadConflict, UploadNotFoundOrExpired, MediaStorageFailure, MediaRecordFailure):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MediaStorageFailure("upload completion persistence failed") from exc

    async def _merge_in_workspace(
        self,
        session: UploadSession,
        upload_id: str,
        user_id: int,
        workspace: MediaWorkspace,
        lease: MergeLockLease,
    ) -> tuple[MediaRecord, int]:
        # The merged file never escapes the active workspace.  It is consumed
        # by the object port before the scope is closed and is not returned in
        # any application value.
        merged_path = workspace.path / "merged.mp4"
        return await self._merge_file(session, upload_id, user_id, merged_path, lease)

    async def _merge_file(
        self,
        session: UploadSession,
        upload_id: str,
        user_id: int,
        merged_path: Path,
        lease: MergeLockLease,
    ) -> tuple[MediaRecord, int]:
        digest = hashlib.md5()
        with merged_path.open("wb") as output:
            for index in range(session.total_chunks):
                await self._refresh_lease(lease)
                object_name = _chunk_object_name(upload_id, index)
                try:
                    stream = self._chunks.read_chunk(object_name)
                    async for piece in stream:
                        if not isinstance(piece, bytes):
                            raise TypeError("chunk store must yield bytes")
                        digest.update(piece)
                        await asyncio.to_thread(output.write, piece)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise MediaStorageFailure("chunk object read failed") from exc

        await self._refresh_lease(lease)
        object_name = _final_object_name(session)
        try:
            source = await self._object_storage.put_object(
                _file_chunks(merged_path),
                object_name=object_name,
                content_type=_content_type(session.filename),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MediaStorageFailure("merged media object upload failed") from exc
        if not isinstance(source, str) or not source.strip():
            raise MediaStorageFailure("merged object storage returned an empty source")
        if source != self._object_storage.source_for(object_name):
            raise MediaStorageFailure("merged object storage returned an unstable source")

        record = MediaRecord(
            user_id=user_id,
            filename=session.filename,
            source=source,
            content_hash=digest.hexdigest(),
            uploaded_at=self._clock.now(),
            content_type=_content_type(session.filename),
        )
        await self._refresh_lease(lease)
        # A cancelled/failed DB call can have committed in its executor thread.
        # Keep the deterministic object for recovery rather than deleting bytes
        # potentially referenced by a durable row.
        saving = asyncio.create_task(self._records.save(record))
        try:
            saved = await asyncio.shield(saving)
        except asyncio.CancelledError:
            # Cancelling to_thread does not cancel a database transaction.
            # Keep the lease until the bounded DB call settles, then propagate
            # cancellation. Retry can recover a row that actually committed.
            try:
                await asyncio.shield(saving)
            except Exception:
                pass
            raise
        if not isinstance(saved, MediaRecord) or saved.media_id is None:
            raise MediaStorageFailure("media record port returned no persisted id")

        return await self._finish_completion(session, saved, lease)

    async def _finish_completion(
        self, session: UploadSession, saved: MediaRecord, lease: MergeLockLease,
    ) -> tuple[MediaRecord, int]:
        await self._refresh_lease(lease)

        marker = CompletedUploadMarker(
            upload_id=session.upload_id,
            user_id=session.user_id,
            media_id=saved.media_id,
            expires_at=self._clock.now() + self._ttl,
            filename=session.filename,
            total_chunks=session.total_chunks,
            created_at=session.created_at,
        )
        # Receipt outage is a visible retryable failure. Durable row/object and
        # valid chunks remain; the next complete recovers this exact result.
        await self._sessions.set_completed(marker)

        return saved, session.total_chunks

    async def _refresh_lease(self, lease: MergeLockLease) -> None:
        try:
            await asyncio.to_thread(lease.refresh)
        except Exception as exc:
            raise MediaStorageFailure("upload merge lease lost; retry complete") from exc

    async def _best_effort_cleanup(self, upload_id: str, total_chunks: int) -> None:
        for index in range(total_chunks):
            try:
                await self._chunks.delete_chunk(_chunk_object_name(upload_id, index))
            except Exception:
                continue
        try:
            await self._sessions.delete_session(upload_id)
        except Exception:
            pass

    async def _require_active(self, upload_id: str, user_id: int) -> UploadSession:
        marker = await self._valid_completed_marker(upload_id, user_id)
        if marker is not None:
            raise UploadConflict("upload has already completed")
        session = await self._sessions.get_session(upload_id)
        if session is None:
            marker = await self._sessions.get_completed(upload_id)
            if marker is not None and marker.user_id != user_id:
                raise MediaUnauthorized("upload is owned by another user")
            if marker is not None:
                raise UploadConflict("upload has already completed")
            raise UploadNotFoundOrExpired("upload does not exist or has expired")
        if not isinstance(session, UploadSession):
            raise UploadNotFoundOrExpired("upload metadata is invalid")
        if session.user_id != user_id:
            raise MediaUnauthorized("upload is owned by another user")
        if session.is_expired(self._clock.now()):
            raise UploadNotFoundOrExpired("upload has expired")
        if session.state is not UploadSessionState.ACTIVE:
            raise UploadConflict("upload is not active")
        return session

    async def _verify_owner(self, upload_id: str, user_id: int) -> None:
        """Reject a known non-owner before lock acquisition.

        Missing/corrupt state is intentionally left for the normal active or
        completed-marker lookup after the lock has been obtained.
        """

        session = await self._sessions.get_session(upload_id)
        if isinstance(session, UploadSession) and session.user_id != user_id:
            raise MediaUnauthorized("upload is owned by another user")
        marker = await self._sessions.get_completed(upload_id)
        if isinstance(marker, CompletedUploadMarker) and marker.user_id != user_id:
            raise MediaUnauthorized("upload is owned by another user")

    async def _sorted_chunk_indexes(self, session: UploadSession) -> tuple[int, ...]:
        try:
            confirmed = await self._sessions.get_uploaded_chunks(session.upload_id)
            if confirmed is None:
                # One-time rolling upgrade for pre-Set sessions only. Fresh
                # sessions never treat unconfirmed MinIO objects as progress.
                confirmed = tuple(await self._chunks.list_chunks(session.upload_id))
                await self._sessions.confirm_chunks(session, confirmed)
            indexes = tuple(confirmed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MediaStorageFailure("chunk listing failed") from exc
        return tuple(sorted(set(indexes)))

    async def _valid_completed_marker(
        self,
        upload_id: str,
        user_id: int,
    ) -> CompletedUploadMarker | None:
        marker = await self._sessions.get_completed(upload_id)
        if marker is None:
            return None
        if not isinstance(marker, CompletedUploadMarker):
            await self._sessions.delete_completed(upload_id)
            return None
        if marker.user_id != user_id:
            raise MediaUnauthorized("upload is owned by another user")
        if marker.is_expired(self._clock.now()):
            await self._sessions.delete_completed(upload_id)
            return None
        return marker

    async def _completed_record(self, upload_id: str, user_id: int) -> MediaRecord | None:
        marker = await self._valid_completed_marker(upload_id, user_id)
        if marker is None:
            return None
        record = await self._records.get(marker.media_id)
        if record is None:
            # A stale/corrupt marker must not make a retry return a phantom.
            await self._sessions.delete_completed(upload_id)
            return None
        if record.user_id != user_id:
            raise MediaUnauthorized("completed media is owned by another user")
        return record


async def _read_limited(
    source: BinaryIO | bytes | bytearray | PathLike[str],
    max_bytes: int,
) -> bytes:
    if isinstance(source, bytes):
        payload = source
        if len(payload) > max_bytes:
            raise MediaPayloadTooLarge("chunk size cannot exceed 5 MiB")
        return payload
    if isinstance(source, bytearray):
        payload = bytes(source)
        if len(payload) > max_bytes:
            raise MediaPayloadTooLarge("chunk size cannot exceed 5 MiB")
        return payload
    if isinstance(source, (str, os.PathLike)):
        stream = Path(os.fspath(source)).open("rb")
        close = True
    elif hasattr(source, "read"):
        stream = source
        close = False
    else:
        raise InvalidMediaInput("chunk must be bytes, a path, or a binary stream")
    payload = bytearray()
    try:
        while len(payload) <= max_bytes:
            piece = await asyncio.to_thread(stream.read, min(STREAM_CHUNK_BYTES, max_bytes + 1 - len(payload)))
            if not piece:
                break
            if not isinstance(piece, (bytes, bytearray, memoryview)):
                raise InvalidMediaInput("chunk input must yield bytes")
            payload.extend(bytes(piece))
            if len(payload) > max_bytes:
                raise MediaPayloadTooLarge("chunk size cannot exceed 5 MiB")
    finally:
        if close:
            await asyncio.to_thread(stream.close)
    return bytes(payload)


async def _file_chunks(path: Path, *, chunk_bytes: int = STREAM_CHUNK_BYTES) -> AsyncIterable[bytes]:
    stream = path.open("rb")
    try:
        while True:
            piece = await asyncio.to_thread(stream.read, chunk_bytes)
            if not piece:
                return
            yield piece
    finally:
        await asyncio.to_thread(stream.close)


def _canonical_upload_id(upload_id: str) -> str:
    if not isinstance(upload_id, str):
        raise InvalidMediaInput("upload_id must be text")
    try:
        return str(UUID(upload_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidMediaInput("upload_id must be a UUID") from exc


def _chunk_object_name(upload_id: str, index: int) -> str:
    return f"{CHUNK_OBJECT_PREFIX}{upload_id}/part-{index}"


def _final_object_name(session: UploadSession) -> str:
    return f"{MEDIA_OBJECT_PREFIX}upload-{session.upload_id}{filename_suffix(session.filename)}"


def _validate_total_chunks(total_chunks: int, maximum: int) -> None:
    if not isinstance(total_chunks, int) or isinstance(total_chunks, bool):
        raise InvalidMediaInput("total_chunks must be an integer")
    if not 1 <= total_chunks <= maximum:
        raise InvalidMediaInput(f"total_chunks must be between 1 and {maximum}")


def _require_user(user_id: int) -> None:
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 0:
        raise MediaUnauthorized("authenticated user is required")


def _content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


__all__ = [
    "CHUNK_CONTENT_TYPE",
    "CHUNK_OBJECT_PREFIX",
    "ChunkUploadService",
]

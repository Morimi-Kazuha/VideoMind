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
    UploadSessionPort,
)
from dovideo.application.ports.media import ObjectStoragePort
from dovideo.application.ports.observability import ClockPort

from .ingest import MEDIA_OBJECT_PREFIX, STREAM_CHUNK_BYTES
from .memory import SystemClock
from .workspace import MediaWorkspace

CHUNK_OBJECT_PREFIX = "chunk-uploads/"
CHUNK_CONTENT_TYPE = "application/octet-stream"


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
            elif (
                marker.filename is None
                or marker.total_chunks is None
                or marker.created_at is None
            ):
                # A marker without the original session shape cannot produce
                # an honest status response.  Drop it and fall through to the
                # active-session lookup instead of inventing total_chunks=1.
                await self._sessions.delete_completed(canonical)
            else:
                # A completed session's active metadata may have been
                # cleaned; the marker carries the original shape when it
                # was written by this implementation.
                session = UploadSession(
                    upload_id=canonical,
                    filename=marker.filename,
                    total_chunks=marker.total_chunks,
                    user_id=user_id,
                    created_at=marker.created_at,
                    expires_at=marker.expires_at,
                    state=UploadSessionState.COMPLETED,
                )
                return UploadStatus(
                    session=session,
                    uploaded_chunks=(),
                    completed_media_id=record.media_id,
                )
        session = await self._require_active(canonical, user_id)
        indexes = await self._sorted_chunk_indexes(canonical)
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
            await self._sessions.renew_session(canonical, renewed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await self._chunks.delete_chunk(object_name)
            except Exception as cleanup_error:
                exc.add_note(f"chunk rollback failed: {cleanup_error!r}")
            raise MediaStorageFailure("upload session renewal failed") from exc

    async def complete(self, upload_id: str, user_id: int) -> MediaRecord:
        """Merge exact indexes once, with a nonblocking per-upload lock."""

        _require_user(user_id)
        canonical = _canonical_upload_id(upload_id)
        # Check ownership before attempting the nonblocking lock.  An
        # unrelated caller must not be able to turn a held lock into a
        # misleading conflict response (and must never learn merge timing).
        await self._verify_owner(canonical, user_id)
        lease = self._merge_lock.try_acquire(canonical)
        if lease is None:
            raise UploadConflict("upload is already being merged")
        try:
            completed = await self._completed_record(canonical, user_id)
            if completed is not None:
                return completed
            session = await self._require_active(canonical, user_id)
            indexes = await self._sorted_chunk_indexes(canonical)
            expected = tuple(range(session.total_chunks))
            if indexes != expected:
                raise UploadConflict(
                    f"upload chunks are incomplete ({len(indexes)}/{session.total_chunks})"
                )

            workspace = MediaWorkspace(parent=self._workspace_parent, prefix="dovideo-merge-")
            result: MediaRecord | None = None
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
        finally:
            lease.release()

    async def _merge_in_workspace(
        self,
        session: UploadSession,
        upload_id: str,
        user_id: int,
        workspace: MediaWorkspace,
    ) -> MediaRecord:
        # The merged file never escapes the active workspace.  It is consumed
        # by the object port before the scope is closed and is not returned in
        # any application value.
        merged_path = workspace.path / "merged.mp4"
        return await self._merge_file(session, upload_id, user_id, merged_path)

    async def _merge_file(
        self,
        session: UploadSession,
        upload_id: str,
        user_id: int,
        merged_path: Path,
    ) -> MediaRecord:
        digest = hashlib.md5()
        with merged_path.open("wb") as output:
            for index in range(session.total_chunks):
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

        object_name = f"{MEDIA_OBJECT_PREFIX}{uuid4().hex}{filename_suffix(session.filename)}"
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

        record = MediaRecord(
            user_id=user_id,
            filename=session.filename,
            source=source,
            content_hash=digest.hexdigest(),
            uploaded_at=self._clock.now(),
            content_type=_content_type(session.filename),
        )
        try:
            saved = await self._records.save(record)
        except asyncio.CancelledError as exc:
            await _rollback_object(self._object_storage, source, exc)
            raise
        except Exception as exc:
            await _rollback_object(self._object_storage, source, exc)
            raise
        if not isinstance(saved, MediaRecord) or saved.media_id is None:
            error = TypeError("media record port returned no persisted id")
            await _rollback_object(self._object_storage, source, error)
            raise error

        marker = CompletedUploadMarker(
            upload_id=upload_id,
            user_id=user_id,
            media_id=saved.media_id,
            expires_at=self._clock.now() + self._ttl,
            filename=session.filename,
            total_chunks=session.total_chunks,
            created_at=session.created_at,
        )
        try:
            await self._sessions.set_completed(marker)
        except asyncio.CancelledError as exc:
            await self._rollback_completed_failure(saved, source, exc)
            raise
        except Exception as exc:
            await self._rollback_completed_failure(saved, source, exc)
            raise

        await self._best_effort_cleanup(upload_id, session.total_chunks)
        return saved

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

    async def _rollback_completed_failure(
        self,
        record: MediaRecord,
        source: str,
        original: BaseException,
    ) -> None:
        await _rollback_object(self._object_storage, source, original)
        if record.media_id is not None:
            try:
                await self._records.delete(record.media_id)
            except Exception as cleanup_error:
                original.add_note(f"media record rollback failed: {cleanup_error!r}")
                setattr(original, "record_cleanup_error", cleanup_error)

    async def _require_active(self, upload_id: str, user_id: int) -> UploadSession:
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

    async def _sorted_chunk_indexes(self, upload_id: str) -> tuple[int, ...]:
        try:
            indexes = tuple(await self._chunks.list_chunks(upload_id))
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


async def _rollback_object(
    storage: ObjectStoragePort,
    source: str,
    original: BaseException,
) -> None:
    try:
        await storage.delete_object(source)
    except Exception as cleanup_error:
        original.add_note(f"merged object rollback failed: {cleanup_error!r}")
        setattr(original, "object_cleanup_error", cleanup_error)


def _canonical_upload_id(upload_id: str) -> str:
    if not isinstance(upload_id, str):
        raise InvalidMediaInput("upload_id must be text")
    try:
        return str(UUID(upload_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidMediaInput("upload_id must be a UUID") from exc


def _chunk_object_name(upload_id: str, index: int) -> str:
    return f"{CHUNK_OBJECT_PREFIX}{upload_id}/part-{index}"


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

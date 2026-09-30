from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dovideo.application import (
    CompletedUploadMarker,
    InvalidMediaInput,
    MediaRecord,
    MediaStorageFailure,
    MediaUnauthorized,
    UploadConflict,
    UploadNotFoundOrExpired,
)
from dovideo.infrastructure.media import (
    CHUNK_OBJECT_PREFIX,
    ChunkUploadService,
    InMemoryChunkObjectStore,
    InMemoryMediaRecordStore,
    InMemoryMergeLock,
    InMemoryObjectStorage,
    InMemoryUploadSessionStore,
    MutableClock,
)


def make_service(tmp_path: Path, *, max_chunk_bytes: int = 5 * 1024 * 1024):
    clock = MutableClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    chunks = InMemoryChunkObjectStore(read_piece_bytes=2)
    objects = InMemoryObjectStorage()
    records = InMemoryMediaRecordStore()
    sessions = InMemoryUploadSessionStore(clock)
    locks = InMemoryMergeLock()
    service = ChunkUploadService(
        chunks,
        objects,
        records,
        sessions,
        locks,
        clock=clock,
        workspace_parent=tmp_path,
        max_chunk_bytes=max_chunk_bytes,
    )
    return service, clock, chunks, objects, records, sessions, locks


@pytest.mark.asyncio
async def test_r6_lost_complete_response_status_retry_and_late_chunk(tmp_path: Path) -> None:
    service, _, chunks, _, records, _, _ = make_service(tmp_path)
    upload_id = await service.initialize('lost.mp4', 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b'video', 11)
    # The server commits; the caller loses the returned value before receiving it.
    await service.complete(upload_id, 11)
    status = await service.status(upload_id, 11)
    first_id = status.completed_media_id
    assert first_id is not None
    recovered = await service.complete(upload_id, 11)
    assert recovered.media_id == first_id
    assert len(records.records) == 1
    with pytest.raises(UploadConflict):
        await service.upload_chunk(upload_id, 0, 1, b'late', 11)
    with pytest.raises(MediaUnauthorized):
        await service.complete(upload_id, 12)
    assert (await service.status(upload_id, 11)).completed_media_id == first_id
    assert not chunks.objects


@pytest.mark.asyncio
async def test_initialize_status_and_out_of_order_duplicate_chunks(tmp_path: Path) -> None:
    service, clock, chunks, objects, records, sessions, _ = make_service(tmp_path)
    session = await service.initialize_session(r"C:\folder\movie.MP4", 3, 11)

    assert session.filename == "movie.MP4"
    assert session.expires_at - session.created_at == timedelta(hours=24)
    assert (await service.status(session.upload_id, 11)).uploaded_chunks == ()

    await service.upload_chunk(session.upload_id, 2, 3, b"C", 11)
    await service.upload_chunk(session.upload_id, 0, 3, b"A", 11)
    await service.upload_chunk(session.upload_id, 1, 3, b"old", 11)
    await service.upload_chunk(session.upload_id, 1, 3, b"B", 11)

    status = await service.status(session.upload_id, 11)
    assert status.uploaded_chunks == (0, 1, 2)
    assert chunks.objects[f"{CHUNK_OBJECT_PREFIX}{session.upload_id}/part-1"] == b"B"
    assert sessions.renew_calls == 4
    assert objects.objects == {}
    assert records.records == {}
    assert clock.now() < session.expires_at


@pytest.mark.asyncio
async def test_chunk_validation_owner_uuid_expiry_and_size(tmp_path: Path) -> None:
    service, clock, _, _, _, _, _ = make_service(tmp_path, max_chunk_bytes=4)
    session = await service.initialize("clip.webm", 1, 5)

    with pytest.raises(MediaUnauthorized):
        await service.upload_chunk(session, 0, 1, b"x", 6)
    with pytest.raises(InvalidMediaInput):
        await service.upload_chunk(session, 0, 1, b"12345", 5)
    with pytest.raises(InvalidMediaInput):
        await service.upload_chunk(session, True, 1, b"x", 5)
    with pytest.raises(InvalidMediaInput):
        await service.status("not-a-uuid", 5)

    clock.advance(timedelta(hours=24))
    with pytest.raises(UploadNotFoundOrExpired):
        await service.status(session, 5)


@pytest.mark.asyncio
async def test_complete_requires_exact_indexes_and_merges_in_numeric_order(tmp_path: Path) -> None:
    service, _, chunks, objects, records, sessions, _ = make_service(tmp_path)
    session = await service.initialize_session("ordered.mkv", 3, 3)
    await service.upload_chunk(session.upload_id, 2, 3, b"third", 3)
    await service.upload_chunk(session.upload_id, 0, 3, b"first", 3)
    with pytest.raises(UploadConflict, match="incomplete"):
        await service.complete(session.upload_id, 3)

    await service.upload_chunk(session.upload_id, 1, 3, b"second", 3)
    result = await service.complete(session.upload_id, 3)
    payload = b"firstsecondthird"
    assert result.content_hash == hashlib.md5(payload).hexdigest()
    assert result.media_id == 1
    assert next(iter(objects.objects.values())) == payload
    assert chunks.objects == {}
    assert sessions.sessions == {}
    assert sessions.completed[session.upload_id].total_chunks == 3
    assert await service.complete(session.upload_id, 3) == result
    assert records.save_calls == 1
    completed_status = await service.status(session.upload_id, 3)
    assert completed_status.session.state.value == "COMPLETED"
    assert completed_status.session.total_chunks == 3
    assert completed_status.completed_media_id == result.media_id


@pytest.mark.asyncio
async def test_complete_lock_is_nonblocking_and_owner_checked(tmp_path: Path) -> None:
    service, _, _, _, _, _, locks = make_service(tmp_path)
    session = await service.initialize_session("clip.mp4", 1, 12)
    held = locks.try_acquire(session.upload_id)
    assert held is not None
    try:
        with pytest.raises(UploadConflict, match="already being merged"):
            await service.complete(session.upload_id, 12)
        with pytest.raises(MediaUnauthorized):
            await service.complete(session.upload_id, 13)
    finally:
        held.release()


@pytest.mark.asyncio
async def test_complete_marker_failure_rolls_back_object_and_record(tmp_path: Path) -> None:
    service, _, chunks, objects, records, sessions, _ = make_service(tmp_path)
    session = await service.initialize_session("rollback.mp4", 1, 2)
    await service.upload_chunk(session.upload_id, 0, 1, b"payload", 2)
    sessions.set_completed_error = RuntimeError("marker unavailable")

    with pytest.raises(RuntimeError, match="marker unavailable"):
        await service.complete(session.upload_id, 2)

    assert objects.objects == {}
    assert records.records == {}
    assert sessions.completed == {}
    # Chunks remain available for a caller retry after a failed marker write.
    assert chunks.objects


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_reverse_success(tmp_path: Path) -> None:
    service, _, chunks, _, records, sessions, _ = make_service(tmp_path)
    session = await service.initialize_session("cleanup.mp4", 1, 2)
    await service.upload_chunk(session.upload_id, 0, 1, b"payload", 2)
    chunks.delete_error = RuntimeError("chunk cleanup unavailable")
    sessions.delete_session_error = RuntimeError("metadata cleanup unavailable")

    result = await service.complete(session.upload_id, 2)
    assert result.media_id in records.records
    assert session.upload_id in sessions.completed
    assert session.upload_id in sessions.sessions


@pytest.mark.asyncio
async def test_expired_completed_marker_is_not_an_idempotency_hit(tmp_path: Path) -> None:
    service, clock, _, _, _, sessions, _ = make_service(tmp_path)
    session = await service.initialize_session("expire.mp4", 1, 2)
    await service.upload_chunk(session.upload_id, 0, 1, b"payload", 2)
    await service.complete(session.upload_id, 2)
    clock.advance(timedelta(hours=24))
    with pytest.raises(UploadNotFoundOrExpired):
        await service.status(session.upload_id, 2)


@pytest.mark.asyncio
async def test_legacy_marker_without_session_shape_is_not_fabricated(tmp_path: Path) -> None:
    service, clock, _, _, records, sessions, _ = make_service(tmp_path)
    session = await service.initialize_session("legacy.mp4", 4, 2)
    record = await records.save(
        MediaRecord(
            user_id=2,
            filename="legacy.mp4",
            source="memory://legacy/source.mp4",
            media_id=99,
            uploaded_at=clock.now(),
        )
    )
    await sessions.set_completed(
        CompletedUploadMarker(
            upload_id=session.upload_id,
            user_id=2,
            media_id=record.media_id or 99,
            expires_at=clock.now() + timedelta(hours=24),
        )
    )

    status = await service.status(session.upload_id, 2)
    assert status.session.state.value == "ACTIVE"
    assert status.session.total_chunks == 4
    assert session.upload_id not in sessions.completed


@pytest.mark.asyncio
async def test_storage_read_failure_is_mapped_and_lock_released(tmp_path: Path) -> None:
    service, _, _, _, _, sessions, locks = make_service(tmp_path)
    session = await service.initialize_session("read.mp4", 1, 2)
    # Bypass the service to model an index that the listing sees but whose
    # object read fails.
    class ListingOnly:
        async def list_chunks(self, upload_id: str) -> tuple[int, ...]:
            return (0,)

        def read_chunk(self, object_name: str):
            async def stream():
                raise OSError("read failed")
                yield b""

            return stream()

        async def put_chunk(self, object_name, chunks, *, size):
            raise AssertionError

        async def delete_chunk(self, object_name):
            return None

    service._chunks = ListingOnly()  # type: ignore[assignment]
    with pytest.raises(MediaStorageFailure, match="chunk object read failed"):
        await service.complete(session.upload_id, 2)
    # The lock can be acquired again after the failed attempt.
    lease = locks.try_acquire(session.upload_id)
    assert lease is not None
    lease.release()
    assert session.upload_id in sessions.sessions

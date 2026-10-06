from __future__ import annotations

import hashlib
import asyncio
from dataclasses import replace
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
async def test_complete_marker_failure_recovers_durable_result_without_remerge(tmp_path: Path) -> None:
    service, _, chunks, objects, records, sessions, _ = make_service(tmp_path)
    session = await service.initialize_session("rollback.mp4", 1, 2)
    await service.upload_chunk(session.upload_id, 0, 1, b"payload", 2)
    sessions.set_completed_error = RuntimeError("marker unavailable")
    records.delete_error = RuntimeError("rollback also unavailable")
    objects.delete_error = RuntimeError("object rollback also unavailable")

    with pytest.raises(MediaStorageFailure, match="persistence failed"):
        await service.complete(session.upload_id, 2)

    assert len(objects.objects) == 1
    assert len(records.records) == 1
    assert sessions.completed == {}
    # Chunks remain available for a caller retry after a failed marker write.
    assert chunks.objects
    first_id = next(iter(records.records))
    sessions.set_completed_error = None
    # A new process/service can recover from the durable source identity.
    restarted = ChunkUploadService(chunks, objects, records, sessions, InMemoryMergeLock(), clock=service._clock)
    recovered = await restarted.complete(session.upload_id, 2)
    assert recovered.media_id == first_id
    assert records.save_calls == 1
    assert len(objects.put_calls) == 1


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
async def test_chunk_confirmation_is_data_first_and_redis_failure_keeps_retryable_object(tmp_path):
    service, _, chunks, _, _, sessions, _ = make_service(tmp_path)
    upload_id = await service.initialize("confirmation.mp4", 1, 11)
    original = sessions.confirm_chunks
    calls = []

    async def confirm(session, indexes):
        calls.append(indexes)
        assert chunks.objects[f"{CHUNK_OBJECT_PREFIX}{upload_id}/part-0"] == b"data"
        raise RuntimeError("Redis confirmation unavailable")

    sessions.confirm_chunks = confirm
    with pytest.raises(MediaStorageFailure, match="confirmation failed"):
        await service.upload_chunk(upload_id, 0, 1, b"data", 11)
    assert calls == [(0,)]
    assert (await service.status(upload_id, 11)).uploaded_chunks == ()
    assert chunks.objects  # persisted, but not yet confirmed
    sessions.confirm_chunks = original
    await service.upload_chunk(upload_id, 0, 1, b"data", 11)
    await service.upload_chunk(upload_id, 0, 1, b"data", 11)
    assert (await service.status(upload_id, 11)).uploaded_chunks == (0,)
    assert len(chunks.objects) == 1


@pytest.mark.asyncio
async def test_object_write_failure_never_confirms_and_invalid_chunks_never_write(tmp_path):
    service, _, chunks, _, _, sessions, _ = make_service(tmp_path, max_chunk_bytes=4)
    upload_id = await service.initialize("validation.mp4", 2, 11)
    for index, total, payload in [(-1, 2, b"a"), (2, 2, b"a"), (0, 1, b"a"),
                                   (0, 2, b""), (0, 2, b"12345")]:
        with pytest.raises(InvalidMediaInput):
            await service.upload_chunk(upload_id, index, total, payload, 11)
    assert not chunks.put_calls
    chunks.put_error = OSError("MinIO down")
    with pytest.raises(MediaStorageFailure, match="object upload failed"):
        await service.upload_chunk(upload_id, 0, 2, b"data", 11)
    assert sessions.parts[upload_id] == set()
    assert sessions.renew_calls == 0


@pytest.mark.asyncio
async def test_same_cardinality_wrong_indexes_cannot_complete(tmp_path):
    service, _, _, _, records, sessions, _ = make_service(tmp_path)
    upload_id = await service.initialize("indexes.mp4", 2, 11)
    sessions.parts[upload_id] = {0, 2}
    with pytest.raises(UploadConflict, match="incomplete"):
        await service.complete(upload_id, 11)
    assert records.save_calls == 0


@pytest.mark.asyncio
async def test_same_upload_excludes_second_merge_but_different_uploads_can_merge(tmp_path):
    service, _, chunks, _, records, _, _ = make_service(tmp_path)
    first_id = await service.initialize("one.mp4", 1, 11)
    other_id = await service.initialize("two.mp4", 1, 11)
    for upload_id in (first_id, other_id):
        await service.upload_chunk(upload_id, 0, 1, b"video", 11)
    original = chunks.read_chunk
    entered, proceed = asyncio.Event(), asyncio.Event()
    reads = []

    def read(name):
        async def stream():
            reads.append(name)
            if first_id in name:
                entered.set()
                await proceed.wait()
            async for piece in original(name):
                yield piece
        return stream()

    chunks.read_chunk = read
    first = asyncio.create_task(service.complete(first_id, 11))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(UploadConflict, match="already being merged"):
            await service.complete(first_id, 11)
        other = await asyncio.wait_for(service.complete(other_id, 11), 2)
        assert other.media_id is not None and not first.done()
    finally:
        proceed.set()
    result = await first
    assert (await service.complete(first_id, 11)).media_id == result.media_id
    assert records.save_calls == 2
    assert len(reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "final", "record"])
async def test_merge_failure_preserves_confirmed_chunks_and_complete_retry_succeeds(tmp_path, failure):
    service, _, chunks, objects, records, sessions, locks = make_service(tmp_path)
    upload_id = await service.initialize("failure.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)
    target, attribute = {"read": (chunks, "read_error"), "final": (objects, "put_error"),
                         "record": (records, "save_error")}[failure]
    setattr(target, attribute, OSError("failed stage"))
    with pytest.raises(MediaStorageFailure):
        await service.complete(upload_id, 11)
    assert sessions.parts[upload_id] == {0}
    assert chunks.objects
    lease = locks.try_acquire(upload_id)
    assert lease is not None
    lease.release()
    setattr(target, attribute, None)
    result = await service.complete(upload_id, 11)
    assert result.media_id is not None
    assert len(records.records) == 1
    assert len(objects.objects) == 1


@pytest.mark.asyncio
async def test_database_commit_then_response_failure_recovers_one_result(tmp_path):
    service, _, _, objects, records, _, _ = make_service(tmp_path)
    upload_id = await service.initialize("ambiguous.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)
    original = records.save

    async def ambiguous(record):
        await original(record)
        raise OSError("database commit ACK lost")

    records.save = ambiguous
    with pytest.raises(MediaStorageFailure):
        await service.complete(upload_id, 11)
    recovered = await service.complete(upload_id, 11)
    assert recovered.media_id == next(iter(records.records))
    assert records.save_calls == 1
    assert len(objects.put_calls) == 1


@pytest.mark.asyncio
async def test_cancelled_database_save_settles_before_lock_release_and_recovers(tmp_path):
    service, _, _, _, records, _, locks = make_service(tmp_path)
    upload_id = await service.initialize("cancel-save.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)
    original = records.save
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def pending(record):
        entered.set()
        await proceed.wait()
        return await original(record)

    records.save = pending
    complete = asyncio.create_task(service.complete(upload_id, 11))
    await asyncio.wait_for(entered.wait(), 2)
    complete.cancel()
    await asyncio.sleep(0)
    assert locks.try_acquire(upload_id) is None
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await complete
    assert (await service.complete(upload_id, 11)).media_id == next(iter(records.records))
    assert records.save_calls == 1


@pytest.mark.asyncio
async def test_merge_deadline_releases_lock_and_preserves_resume(tmp_path):
    service, _, chunks, _, _, sessions, locks = make_service(tmp_path)
    service._merge_timeout_seconds = 0.05
    upload_id = await service.initialize("deadline.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)

    def stalled(name):
        async def stream():
            await asyncio.Event().wait()
            yield b""
        return stream()

    chunks.read_chunk = stalled
    with pytest.raises(MediaStorageFailure, match="deadline exceeded"):
        await service.complete(upload_id, 11)
    assert sessions.parts[upload_id] == {0} and chunks.objects
    lease = locks.try_acquire(upload_id)
    assert lease is not None
    lease.release()


@pytest.mark.asyncio
async def test_receipt_owner_and_durable_media_owner_are_both_checked(tmp_path):
    service, clock, _, _, records, sessions, locks = make_service(tmp_path)
    upload_id = await service.initialize("owner.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)
    for action in [lambda: service.status(upload_id, 12),
                   lambda: service.upload_chunk(upload_id, 0, 1, b"foreign", 12),
                   lambda: service.complete(upload_id, 12)]:
        with pytest.raises(MediaUnauthorized):
            await action()
    result = await service.complete(upload_id, 11)
    sessions.completed[upload_id] = replace(sessions.completed[upload_id], user_id=12)
    for action in [lambda: service.status(upload_id, 12), lambda: service.complete(upload_id, 12)]:
        with pytest.raises(MediaUnauthorized):
            await action()
    assert len(records.records) == 1


@pytest.mark.asyncio
async def test_cleanup_failure_still_rejects_late_chunk_and_confirms_same_receipt(tmp_path):
    service, _, chunks, objects, records, sessions, _ = make_service(tmp_path)
    upload_id = await service.initialize("cleanup-late.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)
    sessions.delete_session_error = RuntimeError("cleanup failed")
    chunks.delete_error = RuntimeError("cleanup failed")
    first = await service.complete(upload_id, 11)
    with pytest.raises(UploadConflict):
        await service.upload_chunk(upload_id, 0, 1, b"late", 11)
    assert (await service.complete(upload_id, 11)).media_id == first.media_id
    assert records.save_calls == 1 and len(objects.put_calls) == 1


@pytest.mark.asyncio
async def test_in_flight_chunk_confirmation_cannot_resurrect_completed_session(tmp_path):
    service, _, chunks, _, _, sessions, _ = make_service(tmp_path)
    upload_id = await service.initialize("late-confirm.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)
    original = chunks.put_chunk
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        await proceed.wait()

    chunks.put_chunk = pending
    late = asyncio.create_task(service.upload_chunk(upload_id, 0, 1, b"video", 11))
    await asyncio.wait_for(entered.wait(), 2)
    await service.complete(upload_id, 11)
    proceed.set()
    with pytest.raises(UploadConflict):
        await late
    assert upload_id not in sessions.sessions and upload_id not in sessions.parts


@pytest.mark.asyncio
async def test_cleanup_deadline_cannot_fail_a_committed_completion(tmp_path, monkeypatch):
    from dovideo.infrastructure.media import uploads as module
    service, _, _, _, records, sessions, _ = make_service(tmp_path)
    upload_id = await service.initialize("cleanup-timeout.mp4", 1, 11)
    await service.upload_chunk(upload_id, 0, 1, b"video", 11)

    async def stalled_cleanup(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "CLEANUP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(service, "_best_effort_cleanup", stalled_cleanup)
    result = await asyncio.wait_for(service.complete(upload_id, 11), 2)
    assert result.media_id == sessions.completed[upload_id].media_id
    assert (await service.complete(upload_id, 11)).media_id == result.media_id
    assert records.save_calls == 1


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
    assert status.session.state.value == "COMPLETED"
    assert status.session.total_chunks == 4
    assert session.upload_id in sessions.completed
    assert (await service.complete(session.upload_id, 2)).media_id == record.media_id


@pytest.mark.asyncio
async def test_storage_read_failure_is_mapped_and_lock_released(tmp_path: Path) -> None:
    service, _, _, _, _, sessions, locks = make_service(tmp_path)
    session = await service.initialize_session("read.mp4", 1, 2)
    sessions.parts[session.upload_id] = {0}
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

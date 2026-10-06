"""Opt-in real Redis/MinIO/MySQL regressions; no provider/model calls.

Enable with scripts/run_upload_live_tests.py. All writes use generated users,
UUID uploads and deterministic object keys; teardown touches only those IDs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError, LockNotOwnedError

from dovideo.application import MediaStorageFailure, MediaUnauthorized, UploadConflict, UploadNotFoundOrExpired
from dovideo.infrastructure import R2Settings, RedisAuthService, create_r2_infrastructure
from dovideo.infrastructure.media.uploads import ChunkUploadService, _chunk_object_name, _final_object_name
from dovideo.infrastructure.redis import RedisMergeLock
from dovideo.presentation.api import create_app
from dovideo.presentation.api.r2_runtime import ProductionR2Services

pytestmark = pytest.mark.skipif(os.environ.get("DOVIDEO_UPLOAD_LIVE") != "1", reason="explicit live infrastructure opt-in required")


@pytest.fixture
def live(tmp_path):
    settings = replace(R2Settings.from_environment(require_production=True), media_workspace=tmp_path)
    first = create_r2_infrastructure(settings)
    second = create_r2_infrastructure(settings)
    asyncio.run(first.initialize())
    asyncio.run(second.initialize())
    auth = RedisAuthService(first.user_store, first.redis_client)
    credentials = f"upload-proof-{secrets.token_hex(8)}", secrets.token_urlsafe(24)
    user = auth.register(*credentials, "upload proof")
    user_id = int(user.user_info.id)
    uploads = []

    def service(infra=first):
        return ChunkUploadService(infra.chunk_store, infra.object_storage, infra.media_repository,
                                  infra.upload_sessions, infra.merge_lock, workspace_parent=tmp_path)

    async def init(total=2, infra=first):
        session = await service(infra).initialize_session("proof.mp4", total, user_id)
        uploads.append(session)
        return session

    yield first, second, user_id, service, init, credentials

    async def cleanup():
        for session in uploads:
            source = first.object_storage.source_for(_final_object_name(session))
            record = await first.media_repository.get_by_source(source)
            if record is not None:
                await first.media_repository.delete(record.media_id)
            await first.object_storage.delete_object(source)
            for index in range(session.total_chunks):
                await first.chunk_store.delete_chunk(_chunk_object_name(session.upload_id, index))
            await first.upload_sessions.delete_session(session.upload_id)
            await first.upload_sessions.delete_completed(session.upload_id)
            first.redis_client.delete(first.merge_lock.redis_key(session.upload_id))
        first.user_store.delete(user_id)

    try:
        asyncio.run(cleanup())
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_live_data_first_set_duplicate_ttl_and_redis_ack_failure(live, monkeypatch):
    first, _, owner, service, init, _ = live
    session = await init()
    upload = service()

    async def failed_confirmation(metadata, indexes):
        assert first.minio_client.stat_object(first.settings.minio_bucket, _chunk_object_name(session.upload_id, 0)).size == 5
        assert first.redis_client.smembers(first.upload_sessions._parts_key(session.upload_id)) == set()
        raise MediaStorageFailure("injected Redis outage after actual MinIO success")

    with monkeypatch.context() as patch:
        patch.setattr(first.upload_sessions, "confirm_chunks", failed_confirmation)
        with pytest.raises(MediaStorageFailure):
            await upload.upload_chunk(session.upload_id, 0, 2, b"first", owner)
    assert (await upload.status(session.upload_id, owner)).uploaded_chunks == ()
    await upload.upload_chunk(session.upload_id, 0, 2, b"first", owner)
    await upload.upload_chunk(session.upload_id, 0, 2, b"first", owner)
    await upload.upload_chunk(session.upload_id, 1, 2, b"second", owner)
    assert (await upload.status(session.upload_id, owner)).uploaded_chunks == (0, 1)
    assert first.redis_client.type(first.upload_sessions._parts_key(session.upload_id)) == b"set"
    assert first.redis_client.smembers(first.upload_sessions._parts_key(session.upload_id)) == {b"0", b"1"}
    for key in [first.upload_sessions._session_key(session.upload_id), first.upload_sessions._parts_key(session.upload_id)]:
        assert 23 * 3600 < first.redis_client.ttl(key) <= 24 * 3600


def test_live_library_lock_cross_client_scope_expiry_and_safe_release(live):
    first, second, owner, service, init, _ = live
    session = asyncio.run(init())
    other = asyncio.run(init())
    held = first.merge_lock.try_acquire(session.upload_id)
    assert held is not None
    try:
        assert second.merge_lock.try_acquire(session.upload_id) is None
        parallel = second.merge_lock.try_acquire(other.upload_id)
        assert parallel is not None
        parallel.refresh()
        parallel.release()
        held.refresh()
        assert 29 * 60 < first.redis_client.ttl(first.merge_lock.redis_key(session.upload_id)) <= 30 * 60
    finally:
        held.release()
    short = RedisMergeLock(first.redis_client, ttl_ms=100)
    expired = short.try_acquire(session.upload_id)
    assert expired is not None
    time.sleep(0.15)
    replacement = second.merge_lock.try_acquire(session.upload_id)
    assert replacement is not None
    try:
        with pytest.raises(LockNotOwnedError):
            expired.refresh()
        expired.release()
        assert first.merge_lock.try_acquire(session.upload_id) is None
    finally:
        replacement.release()


@pytest.mark.asyncio
async def test_live_receipt_failure_and_restart_recovers_committed_mysql_row(live, monkeypatch):
    first, second, owner, service, init, _ = live
    session = await init()
    upload = service()
    await upload.upload_chunk(session.upload_id, 1, 2, b"second", owner)
    await upload.upload_chunk(session.upload_id, 0, 2, b"first", owner)

    async def failed_receipt(marker):
        raise MediaStorageFailure("injected receipt failure after actual MySQL commit")

    with monkeypatch.context() as patch:
        patch.setattr(first.upload_sessions, "set_completed", failed_receipt)
        with pytest.raises(MediaStorageFailure):
            await upload.complete(session.upload_id, owner)
    records = await first.media_repository.list_owned(owner)
    assert len(records) == 1
    first_id = records[0].media_id

    def no_second_merge(*args, **kwargs):
        raise AssertionError("committed result must bypass chunk reads")

    with monkeypatch.context() as patch:
        patch.setattr(second.chunk_store, "read_chunk", no_second_merge)
        recovered = await service(second).complete(session.upload_id, owner)
    assert recovered.media_id == first_id
    assert recovered.content_hash == hashlib.md5(b"firstsecond").hexdigest()
    assert len(await second.media_repository.list_owned(owner)) == 1
    assert (await upload.status(session.upload_id, owner)).completed_media_id == first_id
    assert not first.redis_client.exists(first.upload_sessions._parts_key(session.upload_id))
    assert 23 * 3600 < first.redis_client.ttl(first.upload_sessions._completed_key(session.upload_id)) <= 24 * 3600
    response = first.minio_client.get_object(first.settings.minio_bucket, _final_object_name(session))
    try:
        assert response.read() == b"firstsecond"
    finally:
        response.close()
        response.release_conn()


@pytest.mark.asyncio
async def test_live_atomic_confirmation_does_not_resurrect_expired_or_completed_state(live):
    first, _, owner, service, init, _ = live
    session = await init(1)
    await service().upload_chunk(session.upload_id, 0, 1, b"video", owner)
    await service().complete(session.upload_id, owner)
    with pytest.raises(UploadConflict):
        await first.upload_sessions.confirm_chunks(session, (0,))
    assert not first.redis_client.exists(first.upload_sessions._session_key(session.upload_id))
    assert not first.redis_client.exists(first.upload_sessions._parts_key(session.upload_id))
    expired = await init(1)
    first.redis_client.delete(first.upload_sessions._session_key(expired.upload_id))
    with pytest.raises(UploadNotFoundOrExpired):
        await first.upload_sessions.confirm_chunks(expired, (0,))
    assert not first.redis_client.exists(first.upload_sessions._parts_key(expired.upload_id))


@pytest.mark.asyncio
async def test_live_legacy_migration_is_one_time_and_fresh_orphans_stay_unconfirmed(live):
    first, _, owner, service, init, _ = live
    session = await init()
    await service().upload_chunk(session.upload_id, 0, 2, b"first", owner)
    key = first.upload_sessions._session_key(session.upload_id)
    legacy = json.loads(first.redis_client.get(key))
    legacy.pop("partsTracked")
    first.redis_client.set(key, json.dumps(legacy), keepttl=True)
    first.redis_client.delete(first.upload_sessions._parts_key(session.upload_id))
    assert (await service().status(session.upload_id, owner)).uploaded_chunks == (0,)
    assert json.loads(first.redis_client.get(key))["partsTracked"] is True
    fresh = await init(1)

    async def orphan():
        yield b"orphan"

    await first.chunk_store.put_chunk(_chunk_object_name(fresh.upload_id, 0), orphan(), size=6)
    assert (await service().status(fresh.upload_id, owner)).uploaded_chunks == ()


@pytest.mark.asyncio
async def test_live_concurrent_facades_same_upload_conflict_different_upload_progress(live, monkeypatch):
    first, second, owner, service, init, _ = live
    session, other = await init(1), await init(1)
    for metadata in (session, other):
        await service().upload_chunk(metadata.upload_id, 0, 1, b"video", owner)
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = first.chunk_store.read_chunk

    def paused(name):
        async def stream():
            entered.set()
            await proceed.wait()
            async for piece in original(name):
                yield piece
        return stream()

    with monkeypatch.context() as patch:
        patch.setattr(first.chunk_store, "read_chunk", paused)
        pending = asyncio.create_task(service().complete(session.upload_id, owner))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            with pytest.raises(UploadConflict):
                await service(second).complete(session.upload_id, owner)
            assert (await asyncio.wait_for(service(second).complete(other.upload_id, owner), 5)).media_id is not None
            assert not pending.done()
        finally:
            proceed.set()
        first_result = await pending
    assert (await service(second).complete(session.upload_id, owner)).media_id == first_result.media_id


def test_live_production_http_resume_lost_response_ownership_and_redis_outage(live, monkeypatch):
    first, _, owner, service, init, credentials = live
    session = asyncio.run(init(1))
    services = ProductionR2Services(first)
    # Use real auth + production facade; fixture retains infrastructure cleanup.
    app = create_app(services=services)
    token = services.auth.login(*credentials).token
    headers = {"Authorization": f"Bearer {token}"}
    foreign_credentials = f"upload-foreign-{secrets.token_hex(8)}", secrets.token_urlsafe(24)
    foreign_user = services.auth.register(*foreign_credentials, "foreign proof")
    foreign = services.auth.login(*foreign_credentials).token
    foreign_headers = {"Authorization": f"Bearer {foreign}"}
    client = TestClient(app)
    try:
        form = {"uploadId": session.upload_id, "chunkIndex": 0, "totalChunks": 1}
        for endpoint, method, options in [
            ("/media/upload-status", client.get, {"params": {"uploadId": session.upload_id}}),
            ("/media/upload-chunk", client.post, {"data": form, "files": {"file": ("part", b"video")}}),
            ("/media/complete-upload", client.post, {"params": {"uploadId": session.upload_id}}),
        ]:
            assert method(endpoint, headers=foreign_headers, **options).status_code == 403
        assert client.post("/media/upload-chunk", data=form, files={"file": ("part", b"video")}, headers=headers).status_code == 200
        assert client.get("/media/upload-status", params={"uploadId": session.upload_id}, headers=headers).json()["data"]["uploadedChunks"] == [0]
        first_response = client.post("/media/complete-upload", params={"uploadId": session.upload_id}, headers=headers)
        assert first_response.status_code == 200
        first_id = first_response.json()["data"]["id"]
        # Discard the first response; repeat through actual production routes.
        again = client.post("/media/complete-upload", params={"uploadId": session.upload_id}, headers=headers)
        assert again.status_code == 200 and again.json()["data"]["id"] == first_id
        assert len(client.get("/media/list", headers=headers).json()["data"]) == 1
        assert client.get("/media/upload-status", params={"uploadId": session.upload_id}, headers=foreign_headers).status_code == 403
        assert client.post("/media/upload-chunk", data=form, files={"file": ("part", b"late")}, headers=headers).status_code == 409
        with monkeypatch.context() as patch:
            original = first.redis_client.get

            def unavailable(key, *args, **kwargs):
                if str(key).startswith("upload:"):
                    raise ConnectionError("injected upload Redis outage")
                return original(key, *args, **kwargs)

            patch.setattr(first.redis_client, "get", unavailable)
            outage = client.get("/media/upload-status", params={"uploadId": session.upload_id}, headers=headers)
            assert outage.status_code == 503 and "injected" not in outage.text
    finally:
        services.auth.logout(f"Bearer {token}")
        services.auth.logout(f"Bearer {foreign}")
        first.user_store.delete(int(foreign_user.user_info.id))
        client.close()


@pytest.mark.asyncio
async def test_live_lost_lease_stops_before_final_persistence_and_preserves_chunks(live, monkeypatch):
    first, _, owner, service, init, _ = live
    session = await init(1)
    upload = service()
    await upload.upload_chunk(session.upload_id, 0, 1, b"video", owner)
    original = first.chunk_store.read_chunk
    lock_key = first.merge_lock.redis_key(session.upload_id)

    def ownership_lost(name):
        async def stream():
            async for piece in original(name):
                yield piece
            # Model lease expiry/failover followed by another lock owner.
            first.redis_client.set(lock_key, "replacement-owner", ex=60)
        return stream()

    with monkeypatch.context() as patch:
        patch.setattr(first.chunk_store, "read_chunk", ownership_lost)
        with pytest.raises(MediaStorageFailure, match="lease lost"):
            await upload.complete(session.upload_id, owner)
    assert first.redis_client.get(lock_key) == b"replacement-owner"
    assert not await first.media_repository.list_owned(owner)
    assert (await upload.status(session.upload_id, owner)).uploaded_chunks == (0,)
    first.redis_client.delete(lock_key)
    assert (await upload.complete(session.upload_id, owner)).media_id is not None


@pytest.mark.asyncio
async def test_live_real_five_mib_chunks_concurrent_confirmation_and_ordered_hash(live):
    first, second, owner, service, init, _ = live
    session = await init(4)
    payloads = [bytes([index + 1]) * (5 * 1024 * 1024) for index in range(3)] + [b"last-short-chunk"]
    # Three real 5 MiB writes in flight; confirmation scripts must not lose
    # indexes or shorten TTL when their responses arrive out of order.
    await asyncio.gather(*(service().upload_chunk(session.upload_id, index, 4, payloads[index], owner)
                           for index in (2, 0, 1)))
    await service().upload_chunk(session.upload_id, 3, 4, payloads[3], owner)
    assert (await service(second).status(session.upload_id, owner)).uploaded_chunks == (0, 1, 2, 3)
    result = await service(second).complete(session.upload_id, owner)
    digest = hashlib.md5()
    for payload in payloads:
        digest.update(payload)
    assert result.content_hash == digest.hexdigest()
    response = first.minio_client.get_object(first.settings.minio_bucket, _final_object_name(session))
    try:
        for payload in payloads:
            assert response.read(len(payload)) == payload
        assert response.read(1) == b""
    finally:
        response.close()
        response.release_conn()
    assert (await service().complete(session.upload_id, owner)).media_id == result.media_id
    assert len(await first.media_repository.list_owned(owner)) == 1

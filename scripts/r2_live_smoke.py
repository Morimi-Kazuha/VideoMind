"""Bounded live R2 verification; requires an explicit production env.

This script deliberately prints only stable pass/fail markers. It never
prints environment values, Redis payloads, object credentials, or provider
responses.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from dovideo.application import MediaRecord
from dovideo.application.errors import MediaUnauthorized
from dovideo.application.value_objects import TaskKey
from dovideo.domain import TaskEvent, TaskStage, TaskStatusState, VideoChunk
from dovideo.infrastructure import (
    R2Infrastructure,
    R2Settings,
    RedisAgentTelemetry,
    RedisAuthService,
    create_r2_infrastructure,
)
from dovideo.infrastructure.media.uploads import ChunkUploadService


async def main() -> None:
    settings = R2Settings.from_environment(require_production=True)
    infrastructure = create_r2_infrastructure(settings)
    infrastructure2: R2Infrastructure | None = None
    user_id: int | None = None
    media_id: int | None = None
    upload_id: str | None = None
    object_sources: list[str] = []
    qdrant_media_id = 0
    try:
        await infrastructure.initialize()
        print("R2_INFRA_HEALTH=PASS")

        redis = infrastructure.redis_client
        users = infrastructure.user_store
        auth = RedisAuthService(users, redis)
        username = f"r2-live-{uuid4().hex[:20]}"
        password = "R2-local-auth-check-2026"
        registered = auth.register(username, password, "R2 live")
        user_id = int(registered.user_info.id)
        logged_in = auth.login(username, password)
        assert logged_in.token
        assert auth.require(f"Bearer {logged_in.token}")["id"] == user_id
        auth.logout(f"Bearer {logged_in.token}")
        _verify_login_throttle(auth, redis, username)
        print("R2_REDIS_AUTH=PASS")

        key = TaskKey(900000001, "R2 distributed lock proof")
        first_marker = infrastructure.active_marker
        assert await first_marker.reserve(key, ttl_seconds=60)
        assert not await first_marker.reserve(key, ttl_seconds=60)
        await first_marker.refresh(key, ttl_seconds=60)
        await first_marker.release(key)
        token = await infrastructure.task_lock.acquire(key)
        assert isinstance(token, str)
        assert await infrastructure.task_lock.acquire(key) is None
        await infrastructure.task_lock.release(key, f"{token}-wrong")
        assert await infrastructure.task_lock.acquire(key) is None
        await infrastructure.task_lock.release(key, token)
        replacement_for_key = await infrastructure.task_lock.acquire(key)
        assert isinstance(replacement_for_key, str)
        await infrastructure.task_lock.release(key, replacement_for_key)
        # A second lock is explicitly released with its own token.
        replacement = await infrastructure.task_lock.acquire(TaskKey(900000002, "second lock"))
        assert isinstance(replacement, str)
        await infrastructure.task_lock.release(TaskKey(900000002, "second lock"), replacement)
        print("R2_REDIS_MARKER_LOCK=PASS")

        upload_service = ChunkUploadService(
            infrastructure.chunk_store,
            infrastructure.object_storage,
            infrastructure.media_repository,
            infrastructure.upload_sessions,
            infrastructure.merge_lock,
            workspace_parent=settings.media_workspace,
        )
        session = await upload_service.initialize_session("r2-live.mp4", 2, user_id)
        upload_id = session.upload_id
        try:
            await upload_service.status(upload_id, user_id + 1)
        except MediaUnauthorized:
            pass
        else:
            raise AssertionError("upload ownership was not enforced")
        await upload_service.upload_chunk(upload_id, 0, 2, b"R2-CHUNK-0", user_id)
        await upload_service.upload_chunk(upload_id, 0, 2, b"R2-CHUNK-0", user_id)
        await upload_service.upload_chunk(upload_id, 1, 2, b"R2-CHUNK-1", user_id)
        status = await upload_service.status(upload_id, user_id)
        assert status.uploaded_chunks == (0, 1)
        record = await upload_service.complete(upload_id, user_id)
        duplicate = await upload_service.complete(upload_id, user_id)
        assert duplicate.media_id == record.media_id
        assert record.source.startswith("minio://")
        object_sources.append(record.source)
        media_id = int(record.media_id)
        _stat_object(infrastructure, record.source)
        print("R2_MINIO_UPLOAD_IDEMPOTENCE=PASS")

        checkpoint_key = "r2-live-checkpoint"
        redis_key = f"r2-live:checkpoint:{media_id}"
        infrastructure.checkpoint_repository.write_standalone(
            media_id,
            checkpoint_key,
            redis_key,
            "payload",
            TaskStage.CONTEXT_COMPLETED,
            {"proof": "mysql-then-redis"},
        )
        assert infrastructure.checkpoint_cache.snapshot(redis_key)
        infrastructure.checkpoint_cache.delete_media(media_id)
        recovered = infrastructure.checkpoint_store.read(media_id, checkpoint_key)
        assert recovered is not None and recovered.payload is not None
        print("R2_MYSQL_CHECKPOINT_CACHE_RECOVERY=PASS")

        telemetry = RedisAgentTelemetry(redis)
        telemetry_key = TaskKey(media_id, "R2 trace proof")
        telemetry.start(telemetry_key)
        telemetry.record(
            telemetry_key,
            TaskEvent(state=TaskStatusState.PROCESSING, stage=TaskStage.AGENT_LOOP),
        )
        trace = telemetry.latest(telemetry_key)
        assert trace.get("taskId") == media_id
        assert trace.get("counters", {}).get("AGENT_LOOPCalls") == 1
        print("R2_REDIS_TRACE=PASS")

        qdrant_media_id = media_id
        chunks = (
            VideoChunk(
                startTime=0,
                endTime=300000,
                segmentSummary="early region",
                keywords=("early",),
                embedding=(0.0, 1.0, 0.0),
            ),
            VideoChunk(
                startTime=300000,
                endTime=600000,
                segmentSummary="later semantic region",
                keywords=("later",),
                embedding=(1.0, 0.0, 0.0),
            ),
        )
        await infrastructure.vector_index.upsert(qdrant_media_id, chunks)
        hits = await infrastructure.vector_index.search(
            qdrant_media_id,
            (1.0, 0.0, 0.0),
            limit=2,
        )
        assert len(hits) >= 2 and hits[0].start_ms == 300000
        print("R2_QDRANT_UPSERT_SEARCH=PASS")

        infrastructure.close()
        infrastructure2 = create_r2_infrastructure(settings)
        await infrastructure2.initialize()
        recovered_media = await infrastructure2.media_repository.get(media_id)
        assert recovered_media is not None
        _stat_object(infrastructure2, recovered_media.source)
        hits_after_recreate = await infrastructure2.vector_index.search(
            qdrant_media_id,
            (1.0, 0.0, 0.0),
            limit=2,
        )
        assert hits_after_recreate and hits_after_recreate[0].start_ms == 300000
        print("R2_RESTART_RECOVERY=PASS")

        await infrastructure2.vector_index.delete_media(qdrant_media_id)
        infrastructure2.checkpoint_store.delete_media(media_id)
        await infrastructure2.media_repository.delete(media_id)
        for source in object_sources:
            await infrastructure2.object_storage.delete_object(source)
        if upload_id is not None:
            await infrastructure2.upload_sessions.delete_completed(upload_id)
        if user_id is not None:
            users2 = infrastructure2.user_store
            users2.delete(user_id)
        infrastructure2.redis_client.delete(
            f"auth:login-failures:{__import__('hashlib').sha256(username.encode('utf-8')).hexdigest()}"
        )
        print("R2_LIVE_CLEANUP=PASS")
    finally:
        if infrastructure2 is not None:
            infrastructure2.close()
        else:
            infrastructure.close()


def _verify_login_throttle(auth: RedisAuthService, redis: object, username: str) -> None:
    from dovideo.presentation.api.runtime import R1ServiceError

    unknown = username + "-wrong"
    for _ in range(8):
        try:
            auth.login(unknown, "wrong")
        except R1ServiceError as exc:
            assert exc.status_code == 401
    try:
        auth.login(unknown, "wrong")
    except R1ServiceError as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("Redis login throttle did not trip")
    redis.delete(auth._failure_key(unknown))


def _stat_object(infrastructure: R2Infrastructure, source: str) -> None:
    name = source.split("/", 3)[-1]
    infrastructure.minio_client.stat_object(infrastructure.settings.minio_bucket, name)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"R2_LIVE_SMOKE=FAIL:{type(exc).__name__}")
        raise SystemExit(1) from exc
    print("R2_LIVE_SMOKE=PASS")

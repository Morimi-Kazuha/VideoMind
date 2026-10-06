"""Opt-in real MySQL/Redis/MinIO checks; generated databases, objects and keys."""
import asyncio
import hashlib
import io
import os
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from dovideo.application import AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan, VideoContextBuilder
from dovideo.application.content_context import ContentContextKey, ContentContextPreparation
from dovideo.infrastructure.content_context import RedisContentBuildLock
from dovideo.infrastructure.persistence.content_context import SqlAlchemyContentArtifacts
from dovideo.infrastructure.persistence.migrations import upgrade_schema, require_schema_head
from dovideo.infrastructure.persistence.sqlalchemy import Base, MediaRow, create_sqlalchemy_engine

pytestmark = pytest.mark.skipif(os.environ.get("DOVIDEO_ENGINEERING_LIVE") != "1", reason="explicit engineering infrastructure opt-in required")


@pytest.fixture
def live_database():
    admin_url = make_url(os.environ["DOVIDEO_ENGINEERING_ADMIN_URL"])
    name = "videomind_engineering_" + uuid4().hex[:16]
    assert name.startswith("videomind_engineering_") and len(name) == 38
    admin = create_sqlalchemy_engine(admin_url.render_as_string(hide_password=False))
    with admin.begin() as connection:
        connection.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    engine = create_sqlalchemy_engine(admin_url.set(database=name).render_as_string(hide_password=False))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f"DROP DATABASE `{name}`"))
        admin.dispose()


def no_drift(engine):
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata) == []


def test_live_mysql_empty_repeat_current_crud(live_database):
    upgrade_schema(live_database)
    upgrade_schema(live_database)
    require_schema_head(live_database)
    no_drift(live_database)
    with Session(live_database) as session, session.begin():
        row = MediaRow(user_id=1, filename="generated", status="UPLOADED", file_path="minio://media/generated")
        session.add(row)
        session.flush()
        assert row.id > 0


def test_live_mysql_legacy_text_widening_and_data_retention(live_database):
    with live_database.begin() as connection:
        connection.execute(text("CREATE TABLE agent_checkpoints (media_id BIGINT NOT NULL, checkpoint_key VARCHAR(160) NOT NULL, payload TEXT NULL, PRIMARY KEY(media_id, checkpoint_key))"))
        connection.execute(text("INSERT INTO agent_checkpoints VALUES (10, 'media:context', :payload)"), {"payload": "historical payload"})
        connection.execute(text("CREATE TABLE media_files (id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, user_id BIGINT NOT NULL, filename VARCHAR(255) NOT NULL, status VARCHAR(32) NOT NULL, file_path VARCHAR(1024) NOT NULL, content_hash VARCHAR(64) NULL, upload_time DATETIME NOT NULL)"))
        connection.execute(text("INSERT INTO media_files VALUES (10, 1, 'owner', 'UPLOADED', 'minio://media/owner', 'legacy-md5', CURRENT_TIMESTAMP)"))
    upgrade_schema(live_database)
    upgrade_schema(live_database)
    no_drift(live_database)
    payload = next(column for column in inspect(live_database).get_columns("agent_checkpoints") if column["name"] == "payload")
    assert type(payload["type"]).__name__.upper() == "LONGTEXT"
    with live_database.begin() as connection:
        assert connection.execute(text("SELECT payload FROM agent_checkpoints WHERE media_id=10")).scalar() == "historical payload"
        # Exercise payload size that the old MySQL TEXT could not hold.
        large = "x" * 100_000
        connection.execute(text("UPDATE agent_checkpoints SET payload=:payload WHERE media_id=10"), {"payload": large})
        assert connection.execute(text("SELECT LENGTH(payload) FROM agent_checkpoints WHERE media_id=10")).scalar() == len(large)


@pytest.fixture
def live_redis():
    from redis import Redis
    client = Redis.from_url(os.environ["DOVIDEO_REDIS_URL"], socket_timeout=2, socket_connect_timeout=2)
    client.ping()
    yield client
    client.close()


@pytest.mark.asyncio
async def test_live_mysql_redis_content_concurrency_lifetime_and_stale_hint(live_database, live_redis):
    upgrade_schema(live_database)
    key = ContentContextKey(hashlib.sha256(uuid4().bytes).hexdigest(), "live-r4-contract-v1")
    store = SqlAlchemyContentArtifacts(live_database, live_redis)
    lock = RedisContentBuildLock(live_redis, ttl_ms=300)
    service = ContentContextPreparation(store, lock, poll_seconds=.01)
    calls = 0
    async def build():
        nonlocal calls
        calls += 1
        await asyncio.sleep(.85)
        bundle = MediaObservationBundle(AsrBranchOutcome((TranscriptSpan(0, 5000, "Opening"),), attempted=1), OcrBranchOutcome())
        return VideoContextBuilder().build(key.artifact_source, "", bundle, media_content_identity=key.media_identity)
    try:
        a, b = await asyncio.gather(service.prepare(key, "minio://media/user-A", "A", build),
                                    service.prepare(key, "minio://media/user-B", "B", build))
        assert calls == 1 and a.source != b.source and a.source_revision == b.source_revision
        assert await lock.acquire(key) is not None  # Owner released after renewal.
        live_redis.delete(lock.redis_key(key))
        # Stale hint cannot return phantom context from a deleted artifact.
        with live_database.begin() as connection:
            connection.execute(text("DELETE FROM content_context_artifacts"))
        assert await store.load(key) is None
        assert not live_redis.exists(store.hint_key(key))
        await service.prepare(key, b.source, "B", build)
        assert calls == 2
    finally:
        live_redis.delete(lock.redis_key(key), store.hint_key(key))


@pytest.mark.asyncio
async def test_live_content_old_token_cannot_release_new_lease(live_redis):
    key = ContentContextKey(hashlib.sha256(uuid4().bytes).hexdigest(), "lease-live-v1")
    lock = RedisContentBuildLock(live_redis, ttl_ms=150)
    try:
        a = await lock.acquire(key)
        await asyncio.sleep(.25)
        b = await lock.acquire(key)
        assert b and a != b
        assert await lock.refresh(key, a) is False
        await lock.release(key, a)
        assert live_redis.get(lock.redis_key(key)) == b.encode()
        assert await lock.refresh(key, b) is True
    finally:
        live_redis.delete(lock.redis_key(key))


@pytest.mark.asyncio
async def test_live_minio_r4_byte_fingerprint_and_content_reuse(live_database, live_redis, tmp_path):
    from minio import Minio
    from types import SimpleNamespace
    from dovideo.application import AnalysisRequest, MediaRef
    from dovideo.infrastructure.r2_config import R2Settings, _minio_host
    from dovideo.infrastructure.r4_runtime import R4MediaPipeline
    from dovideo.presentation.composition import AnalysisSettings

    upgrade_schema(live_database)
    settings = R2Settings.from_environment()
    host, secure = _minio_host(settings.minio_endpoint, settings.minio_secure)
    client = Minio(host, access_key=settings.minio_access_key, secret_key=settings.minio_secret_key, secure=secure)
    prefix = "engineering-proof/" + uuid4().hex
    objects = (prefix + "/user-A.mp4", prefix + "/user-B.mp4")
    data = b"generated identical video fixture bytes"
    infra = SimpleNamespace(engine=live_database, redis_client=live_redis, minio_client=client,
                            settings=SimpleNamespace(media_workspace=tmp_path, minio_bucket=settings.minio_bucket))
    telemetry = SimpleNamespace(observe=lambda *args: None)
    pipeline = R4MediaPipeline(infra, AnalysisSettings(), telemetry, None)
    calls = 0
    identities = []
    async def build(request, path, key, complete):
        nonlocal calls
        calls += 1
        assert path.read_bytes() == data
        assert key.fingerprint == hashlib.sha256(data).hexdigest()
        identities.append(key)
        complete[0] = True
        bundle = MediaObservationBundle(AsrBranchOutcome((TranscriptSpan(0, 5000, "Opening"),), attempted=1), OcrBranchOutcome())
        return VideoContextBuilder().build(key.artifact_source, "", bundle, media_content_identity=key.media_identity)
    pipeline._build_local = build  # No paid ASR/LLM calls: verify real download/hash/persistence/lock.
    try:
        for object_name in objects:
            client.put_object(settings.minio_bucket, object_name, io.BytesIO(data), len(data))
        a = await pipeline.build_context(AnalysisRequest(MediaRef(10, f"minio://{settings.minio_bucket}/{objects[0]}"), "A"))
        client.remove_object(settings.minio_bucket, objects[0])  # Source object's lifetime is independent.
        b = await pipeline.build_context(AnalysisRequest(MediaRef(57, f"minio://{settings.minio_bucket}/{objects[1]}"), "B"))
        assert calls == 1 and a.source_revision == b.source_revision
        assert objects[0] not in b.model_dump_json()
    finally:
        for object_name in objects:
            client.remove_object(settings.minio_bucket, object_name)
        for identity in identities:
            live_redis.delete(pipeline.context_preparation.lock.redis_key(identity),
                              pipeline.context_preparation.artifacts.hint_key(identity))

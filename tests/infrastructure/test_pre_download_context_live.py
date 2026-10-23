"""Opt-in real MySQL/Redis/versioned MinIO identity and zero-download proof."""
import asyncio
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from minio import Minio
from minio.error import S3Error
from minio.versioningconfig import ENABLED, VersioningConfig
from sqlalchemy.orm import Session

from test_engineering_live import live_database, live_redis
from dovideo.application import (
    AnalysisRequest, AsrBranchOutcome, MediaObservationBundle, OcrBranchOutcome,
    OcrObservation, TranscriptSpan,
)
from dovideo.application.content_context import ContentContextKey
from dovideo.domain.provenance import stable_frame_ref
from dovideo.infrastructure.persistence.migrations import upgrade_schema
from dovideo.infrastructure.persistence.sqlalchemy import (
    MediaRow, SqlAlchemyMediaRecordRepository, UserRow,
)
from dovideo.infrastructure.r2_config import R2Settings, _minio_host
from dovideo.infrastructure.r4_media import R4MediaPipeline
from dovideo.presentation.composition import AnalysisSettings

pytestmark = pytest.mark.skipif(
    os.environ.get("DOVIDEO_ENGINEERING_LIVE") != "1",
    reason="explicit engineering infrastructure opt-in required",
)


@pytest.mark.asyncio
async def test_real_versioned_media_hit_uses_zero_get_object_and_zero_preprocessing(
    live_database, live_redis, tmp_path, monkeypatch,
):
    upgrade_schema(live_database)
    settings = R2Settings.from_environment()
    host, secure = _minio_host(settings.minio_endpoint, settings.minio_secure)
    client = Minio(host, access_key=settings.minio_access_key,
                   secret_key=settings.minio_secret_key, secure=secure)
    bucket = "context-proof-" + uuid4().hex
    client.make_bucket(bucket)
    identities = []
    counts = {"get_object": 0, "asr_ocr": 0}

    class CountedMinio:
        def stat_object(self, *args, **kwargs):
            return client.stat_object(*args, **kwargs)

        def get_object(self, *args, **kwargs):
            counts["get_object"] += 1
            return client.get_object(*args, **kwargs)

    try:
        client.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
        payload = b"versioned identity proof " + uuid4().bytes
        fingerprint = hashlib.sha256(payload).hexdigest()
        for name in ("user-a.mp4", "user-b.mp4"):
            result = client.put_object(bucket, name, io.BytesIO(payload), len(payload))
            assert result.version_id and result.version_id != "null"
        with Session(live_database) as session, session.begin():
            session.add_all([UserRow(id=1, username="a", password="hash", nickname="A"),
                             UserRow(id=2, username="b", password="hash", nickname="B")])
            session.flush()
            for media_id, owner, name in ((10, 1, "user-a.mp4"), (57, 2, "user-b.mp4")):
                session.add(MediaRow(id=media_id, user_id=owner, filename=name,
                    status="COMPLETED", file_path=f"minio://{bucket}/{name}",
                    content_hash=fingerprint))
        repository = SqlAlchemyMediaRecordRepository(live_database)
        infra = SimpleNamespace(engine=live_database, redis_client=live_redis,
            minio_client=CountedMinio(), media_repository=repository,
            settings=SimpleNamespace(media_workspace=tmp_path, minio_bucket=bucket))
        pipeline = R4MediaPipeline(infra, AnalysisSettings(), SimpleNamespace(observe=lambda *a: None), None)
        pipeline._whisper_adapter = lambda: None

        async def probe(self, path):
            return SimpleNamespace(seconds=360)

        async def collect(self, source, *, media_identity, **kwargs):
            counts["asr_ocr"] += 1
            assert Path(source).read_bytes() == payload
            key = ContentContextKey(fingerprint, pipeline.pipeline_contract)
            assert media_identity == key.media_identity
            identities.append(key)
            return MediaObservationBundle(
                AsrBranchOutcome((TranscriptSpan(0, 5000, "Versioned content"),), attempted=1),
                OcrBranchOutcome((OcrObservation(1000, "Versioned frame",
                    stable_frame_ref(media_identity, 1000, 0)),), attempted=1),
            )

        # Run production _build_local/context construction; substitute only
        # duration and model observations so the storage proof is deterministic.
        monkeypatch.setattr("dovideo.infrastructure.r4_media.FfprobeDurationAdapter.probe", probe)
        monkeypatch.setattr("dovideo.infrastructure.r4_media.MediaBranchOrchestrator.collect", collect)
        a = (await repository.get(10)).to_ref()
        b = (await repository.get(57)).to_ref()
        first = await pipeline.build_context(AnalysisRequest(a, "goal A", user_id=1))
        assert counts == {"get_object": 1, "asr_ocr": 1}
        counts.update(get_object=0, asr_ocr=0)
        second = await pipeline.build_context(AnalysisRequest(a, "goal B", user_id=1))
        assert counts == {"get_object": 0, "asr_ocr": 0}
        assert second.user_goal == "goal B" and second.source_revision == first.source_revision
        # A different original object needs its own real-byte version proof once.
        rebound = await pipeline.build_context(AnalysisRequest(b, "private B", user_id=2))
        assert counts == {"get_object": 1, "asr_ocr": 0}
        assert rebound.source == b.source and a.source not in rebound.model_dump_json()
        counts.update(get_object=0, asr_ocr=0)
        await pipeline.build_context(AnalysisRequest(b, "another B", user_id=2))
        assert counts == {"get_object": 0, "asr_ocr": 0}

        client.put_object(bucket, "user-a.mp4", io.BytesIO(b"replacement"), 11)
        with pytest.raises(ValueError, match="persisted SHA-256"):
            await pipeline.build_context(AnalysisRequest(a, "replaced", user_id=1))
        client.remove_object(bucket, "user-b.mp4")
        with pytest.raises(S3Error) as deleted:
            await pipeline.build_context(AnalysisRequest(b, "deleted", user_id=2))
        assert deleted.value.code == "NoSuchKey"
        assert counts["asr_ocr"] == 0
        assert not list(tmp_path.rglob("*.mp4"))
    finally:
        for key in identities:
            live_redis.delete(pipeline.context_preparation.lock.redis_key(key),
                              pipeline.context_preparation.artifacts.hint_key(key))
        for obj in client.list_objects(bucket, recursive=True, include_version=True):
            client.remove_object(bucket, obj.object_name, version_id=obj.version_id)
        client.remove_bucket(bucket)

"""Production reuse must prove the authorized object's identity before a hit."""

import asyncio
import hashlib
import io
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from dovideo.application import (
    AnalysisRequest,
    AsrBranchOutcome,
    EvidenceVerificationService,
    MediaObservationBundle,
    MediaRef,
    OcrBranchOutcome,
    OcrObservation,
    TranscriptSpan,
    VideoContextBuilder,
)
from dovideo.application.content_context import ContentContextPreparation
from dovideo.domain import AnalysisEvidence
from dovideo.domain.provenance import stable_frame_ref
from dovideo.infrastructure.persistence.content_context import SqlAlchemyContentArtifacts
from dovideo.infrastructure.persistence.sqlalchemy import (
    ContentContextArtifactRow,
    MediaRow,
    SqlAlchemyMediaRecordRepository,
    UserRow,
    create_schema,
    create_sqlalchemy_engine,
)
from dovideo.infrastructure.r4_media import R4MediaPipeline
from dovideo.presentation.composition import AnalysisSettings


VIDEO = b"identical authorized video bytes"
SHA256 = hashlib.sha256(VIDEO).hexdigest()


class VersionedMinio:
    """A replacement creates a new version; explicit old-version GET stays pinned."""

    def __init__(self):
        self.current = {}
        self.versions = {}
        self.downloads = []
        self.stats = []
        self.after_stat = None

    def put(self, name, data, *, version_id=None):
        version = uuid4().hex if version_id is None else version_id
        self.current[name] = version
        self.versions[name, version] = data
        return version

    def stat_object(self, bucket, name, **kwargs):
        self.stats.append((bucket, name))
        if name not in self.current:
            raise FileNotFoundError("generated object is absent")
        version = self.current[name]
        result = SimpleNamespace(version_id=version, size=len(self.versions[name, version]))
        if self.after_stat is not None:
            self.after_stat(name)
        return result

    def get_object(self, bucket, name, *, version_id=None, **kwargs):
        self.downloads.append((bucket, name, version_id))
        if name not in self.current:
            raise FileNotFoundError("generated object is absent")
        version = self.current[name] if version_id is None else version_id
        response = io.BytesIO(self.versions[name, version])
        response.release_conn = lambda: None
        return response


class ContentLock:
    lease_seconds = 1

    def __init__(self):
        self.held = {}

    async def acquire(self, key):
        if key.digest in self.held:
            return None
        token = object()
        self.held[key.digest] = token
        return token

    async def refresh(self, key, token):
        return self.held.get(key.digest) is token

    async def release(self, key, token):
        if self.held.get(key.digest) is token:
            del self.held[key.digest]


@dataclass
class Runtime:
    pipeline: R4MediaPipeline
    engine: object
    minio: VersionedMinio
    counts: dict

    def request(self, media_id=10, *, goal="current private goal", user_id=None):
        owner = 1 if media_id == 10 else 2
        return AnalysisRequest(
            MediaRef(media_id, f"minio://media/user-{owner}/video.mp4", content_hash=SHA256),
            goal,
            user_id=owner if user_id is None else user_id,
        )

    def reset_counts(self):
        self.minio.downloads.clear()
        self.minio.stats.clear()
        self.counts["asr_ocr"] = 0

    def assert_clean(self):
        assert not list(self.pipeline.infrastructure.settings.media_workspace.rglob("*.mp4"))
        assert not self.pipeline.context_preparation.lock.held


@pytest.fixture
def runtime(tmp_path):
    engine = create_sqlalchemy_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    create_schema(engine)
    client = VersionedMinio()
    with Session(engine) as session, session.begin():
        for owner, media_id in ((1, 10), (2, 57)):
            session.add(UserRow(id=owner, username=f"user-{owner}", password="fixture", nickname="fixture"))
            source = f"user-{owner}/video.mp4"
            client.put(source, VIDEO)
            session.add(MediaRow(id=media_id, user_id=owner, filename="video.mp4", status="COMPLETED",
                                 file_path=f"minio://media/{source}", content_hash=SHA256))
    infrastructure = SimpleNamespace(
        engine=engine,
        redis_client=None,
        minio_client=client,
        media_repository=SqlAlchemyMediaRecordRepository(engine),
        settings=SimpleNamespace(media_workspace=tmp_path / "media", minio_bucket="media"),
    )
    pipeline = R4MediaPipeline(infrastructure, AnalysisSettings(), SimpleNamespace(observe=lambda *a: None), None)
    pipeline.context_preparation = ContentContextPreparation(SqlAlchemyContentArtifacts(engine), ContentLock(), poll_seconds=.005)
    counts = {"asr_ocr": 0}

    async def build(request, path, key, complete):
        counts["asr_ocr"] += 1
        assert path.read_bytes() == VIDEO
        await asyncio.sleep(.02)
        bundle = MediaObservationBundle(
            AsrBranchOutcome((TranscriptSpan(0, 5000, "Opening"),), attempted=1),
            OcrBranchOutcome((OcrObservation(1000, "Slide", stable_frame_ref(key.media_identity, 1000, 0)),), attempted=1),
        )
        complete[0] = True
        return VideoContextBuilder().build(key.artifact_source, "", bundle, media_content_identity=key.media_identity)

    pipeline._build_local = build
    yield Runtime(pipeline, engine, client, counts)
    engine.dispose()


@pytest.mark.asyncio
async def test_verified_repeat_hit_skips_get_object_and_asr_ocr(runtime):
    request = runtime.request()
    first = await runtime.pipeline.build_context(request)
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 1
    assert runtime.minio.downloads[0][2] == runtime.minio.current["user-1/video.mp4"]

    runtime.reset_counts()
    second = await runtime.pipeline.build_context(request)

    assert runtime.minio.downloads == [] and runtime.counts["asr_ocr"] == 0
    assert first == second and runtime.minio.stats
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_cross_user_reuse_rebinds_source_goal_and_valid_evidence(runtime):
    first = await runtime.pipeline.build_context(runtime.request(goal="A private goal"))
    # A different uploaded object must establish its own trusted byte binding.
    runtime.reset_counts()
    await runtime.pipeline.build_context(runtime.request(57, goal="B initial goal"))
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0
    runtime.reset_counts()
    second = await runtime.pipeline.build_context(runtime.request(57, goal="B revised goal"))

    assert runtime.minio.downloads == [] and runtime.counts["asr_ocr"] == 0
    assert second.source == "minio://media/user-2/video.mp4"
    assert second.user_goal == "B revised goal"
    assert second.source_revision == first.source_revision
    assert "user-1/" not in second.model_dump_json() and "A private goal" not in second.model_dump_json()
    segment = second.segments[0]
    item = segment.asr_source_items[0]
    evidence = AnalysisEvidence(timestamp_ms=1000, source="ASR", content="Opening", claim="Opening",
                                source_revision=second.source_revision, segment_id=segment.segment_id,
                                source_item_ids=(item.source_item_id,), source_provenance_version=second.provenance_version)
    guard = EvidenceVerificationService()
    assert guard.supported(second, evidence)
    assert not guard.supported(second, evidence.model_copy(update={"source_revision": "f" * 64}))
    assert not guard.supported(second, evidence.model_copy(update={"source_item_ids": ("forged-item",)}))
    with Session(runtime.engine) as session:
        payload = session.query(ContentContextArtifactRow).one().payload
    assert "minio://" not in payload and "private goal" not in payload
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_client_hash_source_and_filename_never_override_owned_database_media(runtime):
    forged = replace(runtime.request(), media=MediaRef(10, "minio://media/user-2/video.mp4", "fake.mp4", "f" * 64))
    first = await runtime.pipeline.build_context(forged)
    assert runtime.minio.downloads[0][1] == "user-1/video.mp4"
    assert first.source == "minio://media/user-1/video.mp4"
    runtime.reset_counts()
    second = await runtime.pipeline.build_context(forged)
    assert runtime.minio.downloads == [] and runtime.counts["asr_ocr"] == 0
    assert second.source == first.source and second.source_revision == first.source_revision


@pytest.mark.asyncio
async def test_wrong_owner_is_rejected_before_object_or_content_cache_access(runtime):
    await runtime.pipeline.build_context(runtime.request())
    runtime.reset_counts()
    with pytest.raises(ValueError, match="owned"):
        await runtime.pipeline.build_context(runtime.request(user_id=2))
    assert runtime.minio.stats == [] and runtime.minio.downloads == []
    assert runtime.counts["asr_ocr"] == 0


@pytest.mark.asyncio
async def test_missing_media_cannot_use_existing_content_artifact(runtime):
    await runtime.pipeline.build_context(runtime.request())
    with Session(runtime.engine) as session, session.begin():
        session.delete(session.get(MediaRow, 10))
    runtime.reset_counts()
    with pytest.raises(ValueError, match="available"):
        await runtime.pipeline.build_context(runtime.request())
    assert runtime.minio.stats == [] and runtime.minio.downloads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_hash", [hashlib.md5(VIDEO).hexdigest(), None, "", "  ", "not-a-sha256", "f" * 32])
async def test_legacy_or_invalid_database_hash_always_downloads_actual_bytes(runtime, stored_hash):
    with Session(runtime.engine) as session, session.begin():
        session.get(MediaRow, 10).content_hash = stored_hash
    await runtime.pipeline.build_context(runtime.request())
    runtime.reset_counts()
    repeated = await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0
    assert repeated.source == runtime.request().media.source
    with Session(runtime.engine) as session:
        assert session.get(MediaRow, 10).content_hash == stored_hash
        assert session.get(MediaRow, 10).content_identity is None
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_legacy_envelope_without_principal_cannot_establish_fast_path(runtime):
    request = replace(runtime.request(), user_id=None)
    await runtime.pipeline.build_context(request)
    runtime.reset_counts()
    await runtime.pipeline.build_context(request)
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0
    with Session(runtime.engine) as session:
        assert session.get(MediaRow, 10).content_identity is None
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_trusted_hash_mismatch_fails_before_preprocessing_or_publication(runtime):
    runtime.minio.put("user-1/video.mp4", b"different content than trusted database SHA")
    with pytest.raises(ValueError, match="(?i)(sha|hash|fingerprint|content)"):
        await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0
    with Session(runtime.engine) as session:
        assert session.query(ContentContextArtifactRow).count() == 0
        assert session.get(MediaRow, 10).content_identity is None
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_replaced_object_cannot_silently_reuse_previous_content(runtime):
    await runtime.pipeline.build_context(runtime.request())
    runtime.minio.put("user-1/video.mp4", b"changed video")
    runtime.reset_counts()
    with pytest.raises(ValueError, match="(?i)(sha|hash|fingerprint|content)"):
        await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_deleted_current_object_cannot_return_a_durable_cache_hit(runtime):
    await runtime.pipeline.build_context(runtime.request())
    del runtime.minio.current["user-1/video.mp4"]
    runtime.reset_counts()
    with pytest.raises((FileNotFoundError, ValueError)):
        await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0


@pytest.mark.asyncio
async def test_new_version_with_identical_bytes_requires_one_reverification(runtime):
    first = await runtime.pipeline.build_context(runtime.request())
    version = runtime.minio.put("user-1/video.mp4", VIDEO)
    runtime.reset_counts()
    second = await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.minio.downloads[0][2] == version
    assert runtime.counts["asr_ocr"] == 0 and first.source_revision == second.source_revision
    runtime.reset_counts()
    await runtime.pipeline.build_context(runtime.request())
    assert runtime.minio.downloads == [] and runtime.counts["asr_ocr"] == 0


@pytest.mark.asyncio
async def test_pipeline_change_downloads_and_rebuilds_without_reusing_old_artifact(runtime):
    first = await runtime.pipeline.build_context(runtime.request())
    runtime.pipeline.pipeline_contract = "different-pipeline-contract"
    runtime.reset_counts()
    second = await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 1
    assert first.source_revision != second.source_revision


@pytest.mark.asyncio
async def test_corrupt_durable_artifact_rebuilds_under_existing_content_lock(runtime):
    await runtime.pipeline.build_context(runtime.request())
    with Session(runtime.engine) as session, session.begin():
        row = session.query(ContentContextArtifactRow).one()
        payload = json.loads(row.payload)
        payload["source"] = "minio://media/private-old-user"
        row.payload = json.dumps(payload)
        row.payload_digest = hashlib.sha256(row.payload.encode()).hexdigest()
    runtime.reset_counts()
    context = await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 1
    assert context.source == runtime.request().media.source
    assert "private-old-user" not in context.model_dump_json()
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_unbound_cross_user_concurrency_builds_only_once(runtime):
    first, second = await asyncio.gather(runtime.pipeline.build_context(runtime.request()),
                                         runtime.pipeline.build_context(runtime.request(57)))
    # Each distinct object must be byte-verified before it can share a fingerprint.
    assert len(runtime.minio.downloads) == 2 and runtime.counts["asr_ocr"] == 1
    assert first.source != second.source and first.source_revision == second.source_revision
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_verified_cold_content_concurrency_keeps_existing_build_coordination(runtime):
    await runtime.pipeline.build_context(runtime.request())
    await runtime.pipeline.build_context(runtime.request(57))
    with Session(runtime.engine) as session, session.begin():
        session.execute(delete(ContentContextArtifactRow))
    runtime.reset_counts()
    first, second = await asyncio.gather(runtime.pipeline.build_context(runtime.request()),
                                         runtime.pipeline.build_context(runtime.request(57)))
    # A cache miss retains the existing download-before-content-lock boundary.
    assert len(runtime.minio.downloads) == 2 and runtime.counts["asr_ocr"] == 1
    assert first.source != second.source and first.source_revision == second.source_revision
    runtime.assert_clean()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "", "null"])
async def test_unversioned_or_null_version_objects_cannot_enable_fast_hits(runtime, version):
    name = "user-1/video.mp4"
    runtime.minio.current[name] = version
    runtime.minio.versions[name, version] = VIDEO
    await runtime.pipeline.build_context(runtime.request())
    runtime.reset_counts()
    await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0
    assert runtime.minio.downloads[0][2] is None
    with Session(runtime.engine) as session:
        assert session.get(MediaRow, 10).content_identity is None
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_object_replacement_during_fast_lookup_is_detected_before_return(runtime):
    await runtime.pipeline.build_context(runtime.request())
    runtime.reset_counts()
    replaced = False

    def replace_after_first_stat(name):
        nonlocal replaced
        if not replaced:
            replaced = True
            runtime.minio.put(name, b"replaced during cache lookup")

    runtime.minio.after_stat = replace_after_first_stat
    with pytest.raises(ValueError, match="version changed"):
        await runtime.pipeline.build_context(runtime.request())
    assert runtime.minio.downloads == [] and runtime.counts["asr_ocr"] == 0
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_database_owner_change_during_fast_lookup_is_detected_before_return(runtime):
    await runtime.pipeline.build_context(runtime.request())
    runtime.reset_counts()
    original_load = runtime.pipeline.context_preparation.artifacts.load

    async def change_owner_after_artifact_validation(key):
        context = await original_load(key)
        with Session(runtime.engine) as session, session.begin():
            session.get(MediaRow, 10).user_id = 2
        return context

    runtime.pipeline.context_preparation.artifacts.load = change_owner_after_artifact_validation
    with pytest.raises(ValueError, match="owned"):
        await runtime.pipeline.build_context(runtime.request())
    assert runtime.minio.downloads == [] and runtime.counts["asr_ocr"] == 0
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_redis_hint_outage_cannot_bypass_durable_validation(runtime):
    await runtime.pipeline.build_context(runtime.request())

    class UnavailableRedis:
        def get(self, *args, **kwargs):
            raise OSError("Redis unavailable")

        def set(self, *args, **kwargs):
            raise OSError("Redis unavailable")

    runtime.pipeline.context_preparation.artifacts.redis_client = UnavailableRedis()
    runtime.reset_counts()
    context = await runtime.pipeline.build_context(runtime.request())
    assert runtime.minio.downloads == [] and runtime.counts["asr_ocr"] == 0
    assert context.source == runtime.request().media.source


@pytest.mark.asyncio
async def test_head_outage_falls_back_to_real_bytes_instead_of_fast_hit(runtime):
    await runtime.pipeline.build_context(runtime.request())
    runtime.reset_counts()

    def unavailable(*args, **kwargs):
        raise OSError("HEAD unavailable")

    runtime.minio.stat_object = unavailable
    await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 0
    assert runtime.minio.downloads[0][2] is None
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_artifact_lookup_failure_cannot_return_an_unverified_hot_context(runtime):
    await runtime.pipeline.build_context(runtime.request())
    runtime.reset_counts()

    async def unavailable(key):
        raise OSError("artifact lookup unavailable")

    runtime.pipeline.context_preparation.artifacts.load = unavailable
    context = await runtime.pipeline.build_context(runtime.request())
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 1
    assert context.source == runtime.request().media.source
    runtime.assert_clean()


@pytest.mark.asyncio
async def test_cancelled_build_cleans_temporary_video_and_retry_has_private_binding(runtime):
    started = asyncio.Event()
    original_build = runtime.pipeline._build_local

    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()

    runtime.pipeline._build_local = blocked
    task = asyncio.create_task(runtime.pipeline.build_context(runtime.request()))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with Session(runtime.engine) as session:
        assert session.query(ContentContextArtifactRow).count() == 0
    runtime.assert_clean()
    runtime.pipeline._build_local = original_build
    runtime.reset_counts()
    context = await runtime.pipeline.build_context(runtime.request(57, goal="retry private goal"))
    assert context.source == "minio://media/user-2/video.mp4" and context.user_goal == "retry private goal"
    assert len(runtime.minio.downloads) == 1 and runtime.counts["asr_ocr"] == 1
    runtime.assert_clean()

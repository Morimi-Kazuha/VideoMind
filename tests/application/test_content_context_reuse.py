import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from dovideo.application import (
    AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan, OcrObservation, VideoContextBuilder,
)
from dovideo.application.content_context import ContentContextKey, ContentContextPreparation
from dovideo.application.task_lease import TaskLeaseUnavailable
from dovideo.domain.provenance import stable_frame_ref
from dovideo.infrastructure.content_context import RedisContentBuildLock, pipeline_contract
from dovideo.infrastructure.persistence.content_context import SqlAlchemyContentArtifacts
from dovideo.infrastructure.persistence.sqlalchemy import ContentContextArtifactRow, create_sqlalchemy_engine, create_schema


def key(version="v1"):
    return ContentContextKey(hashlib.sha256(b"same video bytes").hexdigest(), version)


def context(identity):
    bundle = MediaObservationBundle(
        AsrBranchOutcome((TranscriptSpan(0, 5000, "Opening"), TranscriptSpan(65000, 70000, "Second")), attempted=2),
        OcrBranchOutcome((OcrObservation(1000, "Slide", stable_frame_ref(identity.media_identity, 1000, 0)),), attempted=1),
    )
    return VideoContextBuilder().build(identity.artifact_source, "", bundle, media_content_identity=identity.media_identity)


class Lock:
    lease_seconds = 0.09
    def __init__(self):
        self.token = None
        self.renewals = 0
        self.releases = 0
        self.unavailable = False
        self.reject = False

    async def acquire(self, identity):
        if self.unavailable:
            raise ConnectionError()
        if self.token is not None:
            return None
        self.token = object()
        return self.token

    async def refresh(self, identity, token):
        self.renewals += 1
        return not self.reject and token is self.token

    async def release(self, identity, token):
        if self.token is token:
            self.token = None
            self.releases += 1


class Hints:
    def __init__(self):
        self.values = {}
        self.unavailable = False
    def set(self, name, value, **kwargs):
        if self.unavailable:
            raise ConnectionError()
        self.values[name] = value
    def get(self, name):
        if self.unavailable:
            raise ConnectionError()
        return self.values.get(name)
    def delete(self, name):
        if self.unavailable:
            raise ConnectionError()
        self.values.pop(name, None)


@pytest.fixture
def artifacts(tmp_path):
    engine = create_sqlalchemy_engine(f"sqlite:///{tmp_path / 'artifacts.db'}")
    create_schema(engine)
    store = SqlAlchemyContentArtifacts(engine, Hints())
    yield store
    engine.dispose()


@pytest.mark.asyncio
async def test_same_media_new_goal_and_cross_user_reuse(artifacts):
    builds = 0
    async def build():
        nonlocal builds
        builds += 1
        return context(key())
    service = ContentContextPreparation(artifacts, Lock(), poll_seconds=.005)
    a = await service.prepare(key(), "minio://media/user-A/media-10.mp4", "A private goal", build)
    revised = await service.prepare(key(), a.source, "New goal", build)
    b = await service.prepare(key(), "minio://media/user-B/media-57.mp4", "B private goal", build)
    assert builds == 1
    assert revised.user_goal == "New goal"
    assert b.source == "minio://media/user-B/media-57.mp4"
    assert b.user_goal == "B private goal"
    assert a.source_revision == b.source_revision
    assert a.segments == b.segments and a.observations == b.observations
    with Session(artifacts.engine) as session:
        payload = session.get(ContentContextArtifactRow, key().digest).payload
    assert "user-A" not in payload and "user-B" not in payload
    assert "private goal" not in payload and "minio://" not in payload


@pytest.mark.asyncio
async def test_concurrent_build_once_and_renewal(artifacts):
    lock = Lock()
    service = ContentContextPreparation(artifacts, lock, poll_seconds=.005)
    builds = 0
    async def build():
        nonlocal builds
        builds += 1
        await asyncio.sleep(.2)  # Longer than the original lease.
        return context(key())
    a, b = await asyncio.gather(service.prepare(key(), "minio://media/a", "a", build),
                                service.prepare(key(), "minio://media/b", "b", build))
    assert builds == 1 and lock.renewals >= 2 and lock.releases == 1
    assert a.source != b.source and a.source_revision == b.source_revision


@pytest.mark.asyncio
async def test_stale_hint_missing_artifact_rebuilds(artifacts):
    await artifacts.publish(key(), context(key()))
    with Session(artifacts.engine) as session, session.begin():
        session.execute(delete(ContentContextArtifactRow))
    assert artifacts.redis_client.values
    assert await artifacts.load(key()) is None
    assert not artifacts.redis_client.values
    result = await ContentContextPreparation(artifacts, Lock()).prepare(key(), "target", "goal", lambda: asyncio.sleep(0, result=context(key())))
    assert result.source == "target" and await artifacts.load(key()) is not None


@pytest.mark.asyncio
async def test_pipeline_version_invalidation(artifacts):
    await artifacts.publish(key(), context(key()))
    changed = key("v2")
    assert await artifacts.load(changed) is None
    b = await ContentContextPreparation(artifacts, Lock()).prepare(changed, "target", "goal", lambda: asyncio.sleep(0, result=context(changed)))
    assert b.source_revision != context(key()).source_revision
    settings = SimpleNamespace(whisper_model="tiny.en", whisper_language="en", whisper_device="cpu")
    assert pipeline_contract(settings, {}) != pipeline_contract(settings, {"DOVIDEO_CONTEXT_PIPELINE_VERSION": "v2"})
    old = pipeline_contract(settings, {})
    settings.whisper_language = "zh"
    assert pipeline_contract(settings, {}) != old


@pytest.mark.asyncio
async def test_redis_failure_allows_private_build_without_publication(artifacts):
    lock = Lock()
    lock.unavailable = True
    artifacts.redis_client.unavailable = True
    result = await ContentContextPreparation(artifacts, lock).prepare(key(), "target", "goal", lambda: asyncio.sleep(0, result=context(key())))
    assert result.source == "target"
    assert await artifacts.load(key()) is None


@pytest.mark.asyncio
async def test_hint_failure_does_not_hide_durable_artifact(artifacts):
    await artifacts.publish(key(), context(key()))
    artifacts.redis_client.unavailable = True
    lock = Lock()
    lock.unavailable = True
    async def forbidden():
        pytest.fail("Durable hit must avoid preprocessing")
    result = await ContentContextPreparation(artifacts, lock).prepare(key(), "target", "goal", forbidden)
    assert result.source == "target"


@pytest.mark.asyncio
async def test_known_contention_timeout_never_builds(artifacts):
    lock = Lock()
    lock.token = object()
    async def forbidden():
        pytest.fail("Known contention must not start a parallel build")
    with pytest.raises(TaskLeaseUnavailable, match="wait expired"):
        await ContentContextPreparation(artifacts, lock, wait_seconds=.02, poll_seconds=.005).prepare(key(), "target", "goal", forbidden)


@pytest.mark.asyncio
async def test_lease_loss_cancels_build_and_never_publishes(artifacts):
    lock = Lock()
    lock.reject = True
    cancelled = asyncio.Event()
    async def build():
        try:
            await asyncio.sleep(1)
            return context(key())
        finally:
            cancelled.set()
    with pytest.raises(TaskLeaseUnavailable, match="ownership lost"):
        await ContentContextPreparation(artifacts, lock).prepare(key(), "target", "goal", build)
    assert cancelled.is_set() and await artifacts.load(key()) is None
    assert lock.releases == 1


@pytest.mark.asyncio
async def test_corrupt_or_private_reference_artifact_is_removed(artifacts):
    await artifacts.publish(key(), context(key()))
    with Session(artifacts.engine) as session, session.begin():
        row = session.get(ContentContextArtifactRow, key().digest)
        raw = json.loads(row.payload)
        raw["source"] = "minio://media/private-owner"
        row.payload = json.dumps(raw)
        row.payload_digest = hashlib.sha256(row.payload.encode()).hexdigest()
    assert await artifacts.load(key()) is None
    with pytest.raises(ValueError, match="private frame"):
        await artifacts.publish(key(), context(key()).model_copy(update={"segments": tuple(
            segment.model_copy(update={"evidence_frames": ("minio://private/frame",)}) for segment in context(key()).segments
        )}))


@pytest.mark.asyncio
async def test_source_media_deletion_does_not_delete_artifact(artifacts):
    from dovideo.infrastructure.persistence.sqlalchemy import MediaRow
    with Session(artifacts.engine) as session, session.begin():
        session.add(MediaRow(id=10, user_id=1, filename="a", status="UPLOADED", file_path="minio://media/user-A"))
    await artifacts.publish(key(), context(key()))
    with Session(artifacts.engine) as session, session.begin():
        session.delete(session.get(MediaRow, 10))
    hit = await artifacts.load(key())
    assert hit == context(key()) and "user-A" not in hit.model_dump_json()


@pytest.mark.asyncio
async def test_partial_preprocessing_kept_private(artifacts):
    service = ContentContextPreparation(artifacts, Lock())
    await service.prepare(key(), "target", "goal", lambda: asyncio.sleep(0, result=context(key())), cacheable=lambda: False)
    assert await artifacts.load(key()) is None


def test_key_requires_sha256_and_content_lock_namespace():
    with pytest.raises(ValueError, match="SHA-256"):
        ContentContextKey("a" * 32, "v1")
    assert RedisContentBuildLock(None).redis_key(key()).startswith("lock:content-context:")

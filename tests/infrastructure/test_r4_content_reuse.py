import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from dovideo.application import AgentCheckpointService, AnalysisRequest, MediaRef
from dovideo.application.content_context import ContentContextPreparation
from dovideo.infrastructure.persistence.content_context import SqlAlchemyContentArtifacts
from dovideo.infrastructure.persistence.repository import CheckpointRepository
from dovideo.infrastructure.persistence.sqlalchemy import SqlAlchemyCheckpointStore, create_schema, create_sqlalchemy_engine
from dovideo.infrastructure.r4_runtime import R4MediaPipeline, R4RequestContextCheckpoint, bind_r4_request, reset_r4_request
from dovideo.presentation.composition import AnalysisSettings
from dovideo.domain import AnalysisEvidence, VideoChunk
from dovideo.application import EvidenceVerificationService
from dovideo.infrastructure.persistence.sqlalchemy import MediaRow, SqlAlchemyMediaRecordRepository

VIDEO_BYTES = b"identical target media bytes"
VIDEO_MD5 = hashlib.md5(VIDEO_BYTES).hexdigest()


class Lock:
    lease_seconds = 10
    def __init__(self):
        self.held = None
    async def acquire(self, key):
        if self.held:
            return None
        self.held = object()
        return self.held
    async def refresh(self, key, token):
        return self.held is token
    async def release(self, key, token):
        if self.held is token:
            self.held = None


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from dovideo.application import AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan, OcrObservation
    from dovideo.domain.provenance import stable_frame_ref
    import dovideo.infrastructure.r4_runtime as r4

    engine = create_sqlalchemy_engine(f"sqlite:///{tmp_path / 'r4.db'}")
    create_schema(engine)
    checkpoint = AgentCheckpointService(CheckpointRepository(SqlAlchemyCheckpointStore(engine)))
    telemetry = SimpleNamespace(store=SimpleNamespace(start_for_request=lambda *args: None), observe=lambda *args: None)
    infrastructure = SimpleNamespace(engine=engine, redis_client=None,
        media_repository=SqlAlchemyMediaRecordRepository(engine),
        settings=SimpleNamespace(media_workspace=tmp_path, minio_bucket="media"))
    events = SimpleNamespace(publish=lambda *args: asyncio.sleep(0))
    pipeline = R4MediaPipeline(infrastructure, AnalysisSettings(), telemetry, events)
    pipeline.context_preparation = ContentContextPreparation(SqlAlchemyContentArtifacts(engine), Lock(), poll_seconds=.005)
    counts = {"download": 0, "asr_ocr": 0}

    async def download(request):
        counts["download"] += 1
        path = tmp_path / f"download-{counts['download']}.mp4"
        path.write_bytes(VIDEO_BYTES)
        return path, VIDEO_MD5
    pipeline._download = download
    pipeline._whisper_adapter = lambda: None
    async def probe(self, path):
        return SimpleNamespace(seconds=360)
    monkeypatch.setattr(r4.FfprobeDurationAdapter, "probe", probe)
    async def collect(self, source, *, media_identity, **kwargs):
        counts["asr_ocr"] += 1
        await asyncio.sleep(.02)
        return MediaObservationBundle(
            AsrBranchOutcome((TranscriptSpan(0, 5000, "Opening"), TranscriptSpan(310000, 315000, "Ending")), attempted=2),
            OcrBranchOutcome((OcrObservation(1000, "Slide", stable_frame_ref(media_identity, 1000, 0)),), attempted=1),
        )
    monkeypatch.setattr(r4.MediaBranchOrchestrator, "collect", collect)
    yield pipeline, checkpoint, counts
    engine.dispose()


async def load(boundary, request):
    token = bind_r4_request(request)
    try:
        return await boundary.load_context(request.media.media_id)
    finally:
        reset_r4_request(token)


async def register(pipeline, request, *, content_hash=VIDEO_MD5):
    with Session(pipeline.infrastructure.engine) as session, session.begin():
        session.add(MediaRow(id=request.media.media_id, user_id=request.media.media_id,
            file_path=request.media.source, filename="video.mp4", status="COMPLETED", content_hash=content_hash))
    return request


@pytest.mark.asyncio
async def test_production_pipeline_accepts_real_short_media_observations(runtime, monkeypatch):
    from dovideo.application import AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan, OcrObservation
    from dovideo.domain.provenance import stable_frame_ref
    import dovideo.infrastructure.r4_runtime as r4
    pipeline, checkpoint, counts = runtime
    async def probe(self, path):
        return SimpleNamespace(seconds=12)
    async def collect(self, source, *, media_identity, **kwargs):
        counts['asr_ocr'] += 1
        return MediaObservationBundle(
            AsrBranchOutcome((TranscriptSpan(0, 8000, 'A short real-media observation'),), attempted=1),
            OcrBranchOutcome((OcrObservation(1000, 'Short clip', stable_frame_ref(media_identity,1000,0)),), attempted=1),
        )
    monkeypatch.setattr(r4.FfprobeDurationAdapter,'probe',probe)
    monkeypatch.setattr(r4.MediaBranchOrchestrator,'collect',collect)
    boundary = R4RequestContextCheckpoint(checkpoint,pipeline)
    request = AnalysisRequest(MediaRef(10,'minio://media/short-video'),'short goal')
    await register(pipeline, request)
    context = await load(boundary,request)
    assert len(context.segments) == 1
    assert context.source == request.media.source
    assert context.source_revision
    assert context.transcript_text() == 'A short real-media observation'
    assert counts == {'download':1,'asr_ocr':1}


@pytest.mark.asyncio
async def test_short_worker_context_prepares_follow_up_inputs_once_without_changing_source(runtime, monkeypatch):
    from unittest.mock import AsyncMock
    from dovideo.application import AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan, OcrObservation
    from dovideo.domain.provenance import stable_frame_ref
    import dovideo.infrastructure.r4_runtime as r4
    pipeline,checkpoint,_=runtime
    async def probe(self,path): return SimpleNamespace(seconds=11)
    async def collect(self,*args,media_identity,**kwargs):
        return MediaObservationBundle(AsrBranchOutcome((TranscriptSpan(0,8000,'short speech'),),attempted=1),OcrBranchOutcome((OcrObservation(1000,'short slide',stable_frame_ref(media_identity,1000,0)),),attempted=1))
    monkeypatch.setattr(r4.FfprobeDurationAdapter,'probe',probe)
    monkeypatch.setattr(r4.MediaBranchOrchestrator,'collect',collect)
    prepare=AsyncMock()
    boundary=R4RequestContextCheckpoint(checkpoint,pipeline,prepare_chunks=prepare)
    request=AnalysisRequest(MediaRef(10,'minio://media/short'),'goal')
    await register(pipeline, request)
    context=await load(boundary,request)
    prepare.assert_awaited_once_with(10,context)
    assert (await checkpoint.load_context(10)).source_revision==context.source_revision


@pytest.mark.asyncio
async def test_actual_r4_pipeline_cross_media_rebinding_and_evidence(runtime):
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    a = AnalysisRequest(MediaRef(10, "minio://media/user-A/video", content_hash="a" * 32), "same goal")
    b = AnalysisRequest(MediaRef(57, "minio://media/user-B/video", content_hash="different-md5"), "same goal")
    await register(pipeline, a)
    await register(pipeline, b)
    first = await load(boundary, a)
    second = await load(boundary, b)
    revised = await load(boundary, AnalysisRequest(a.media, "other goal"))
    assert counts == {"download": 1, "asr_ocr": 1}
    assert first.source == a.media.source and second.source == b.media.source
    assert revised.user_goal == "other goal"
    assert first.source_revision == second.source_revision
    assert first.segments == second.segments
    assert "user-A" not in second.model_dump_json()
    segment = second.segments[0]
    item = segment.asr_source_items[0]
    evidence = AnalysisEvidence(timestamp_ms=1000, source="ASR", content="Opening", claim="Opening",
        source_revision=second.source_revision, segment_id=segment.segment_id,
        source_item_ids=(item.source_item_id,), source_provenance_version=second.provenance_version)
    assert EvidenceVerificationService().supported(second, evidence)
    assert not EvidenceVerificationService().supported(second, evidence.model_copy(update={"source_revision": "f" * 64}))
    assert not EvidenceVerificationService().supported(second, evidence.model_copy(update={"source_item_ids": ("wrong-source-item",)}))
    assert not EvidenceVerificationService().supported(second, evidence.model_copy(update={"segment_id": "wrong-segment"}))
    assert not EvidenceVerificationService().supported(second, evidence.model_copy(update={"source": "OCR"}))
    assert (await checkpoint.load_context(57)).source == b.media.source
    assert not list(pipeline.infrastructure.settings.media_workspace.glob("download-*.mp4"))


@pytest.mark.asyncio
async def test_actual_r4_concurrent_different_media_build_once(runtime):
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    a = AnalysisRequest(MediaRef(10, "minio://media/a"), "a")
    b = AnalysisRequest(MediaRef(57, "minio://media/b"), "b")
    await register(pipeline, a)
    await register(pipeline, b)
    first, second = await asyncio.gather(load(boundary, a), load(boundary, b))
    assert counts["asr_ocr"] == 1 and first.source != second.source
    assert counts["download"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_hash", [None, "", "  ", "invalid-md5", "a" * 64])
async def test_missing_or_invalid_durable_md5_downloads_then_joins_same_cache(runtime, legacy_hash):
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    a = await register(pipeline, AnalysisRequest(MediaRef(10, "minio://media/a"), "A"))
    b = await register(pipeline, AnalysisRequest(MediaRef(57, "minio://media/b", content_hash="f" * 32), "B"), content_hash=legacy_hash)
    first = await load(boundary, a)
    second = await load(boundary, b)
    assert counts == {"download": 2, "asr_ocr": 1}
    assert first.source_revision == second.source_revision and second.source == b.media.source
    assert not list(pipeline.infrastructure.settings.media_workspace.glob("download-*.mp4"))


@pytest.mark.asyncio
async def test_transport_hash_cannot_select_another_content_artifact(runtime):
    from dovideo.application.content_context import ContentContextKey
    pipeline, _, counts = runtime
    a = await register(pipeline, AnalysisRequest(MediaRef(10, "minio://media/a"), "A"))
    await pipeline.build_context(a)
    b = await register(pipeline, AnalysisRequest(MediaRef(57, "minio://media/b", content_hash=VIDEO_MD5), "B"), content_hash="f" * 32)
    # The message hash claims a hit. The durable MD5 requires a miss, and
    # mismatched downloaded bytes must neither run ASR/OCR nor poison its key.
    with pytest.raises(ValueError, match="persisted MD5"):
        await pipeline.build_context(b)
    assert counts == {"download": 2, "asr_ocr": 1}
    assert await pipeline.context_preparation.artifacts.load(ContentContextKey("f" * 32)) is None
    assert not list(pipeline.infrastructure.settings.media_workspace.glob("download-*.mp4"))


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["minio://media/other-user/video", "minio://foreign/video", "minio://media/../video"])
async def test_missing_or_mismatched_media_source_cannot_hit_cache(runtime, source):
    pipeline, _, counts = runtime
    a = await register(pipeline, AnalysisRequest(MediaRef(10, "minio://media/a"), "A"))
    await pipeline.build_context(a)
    with pytest.raises(ValueError, match="record|source"):
        await pipeline.build_context(AnalysisRequest(MediaRef(10, source, content_hash=VIDEO_MD5), "B"))
    with pytest.raises(ValueError, match="record"):
        await pipeline.build_context(AnalysisRequest(MediaRef(999, a.media.source, content_hash=VIDEO_MD5), "B"))
    assert counts == {"download": 1, "asr_ocr": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["https://example.invalid/video", "minio://foreign/video", "minio://media/../video"])
async def test_invalid_durable_source_is_rejected_before_md5_lookup(runtime, source):
    pipeline, _, counts = runtime
    a = await register(pipeline, AnalysisRequest(MediaRef(10, "minio://media/a"), "A"))
    await pipeline.build_context(a)
    b = await register(pipeline, AnalysisRequest(MediaRef(57, source), "B"))
    with pytest.raises(ValueError, match="MinIO|object name"):
        await pipeline.build_context(b)
    assert counts == {"download": 1, "asr_ocr": 1}


@pytest.mark.asyncio
async def test_trusted_md5_hit_never_opens_minio_or_runs_preprocessing(runtime):
    pipeline, _, counts = runtime
    a = await register(pipeline, AnalysisRequest(MediaRef(10, "minio://media/a"), "A"))
    b = await register(pipeline, AnalysisRequest(MediaRef(57, "minio://media/b", content_hash="invalid"), "B"), content_hash=VIDEO_MD5.upper())
    first = await pipeline.build_context(a)
    async def forbidden(*args):
        pytest.fail("A durable MD5 hit must not download or preprocess")
    pipeline._download = pipeline._build_local = forbidden
    second = await pipeline.build_context(b)
    assert second.source == b.media.source and second.user_goal == "B"
    assert first.source_revision == second.source_revision
    assert counts == {"download": 1, "asr_ocr": 1}


@pytest.mark.asyncio
async def test_cancelled_download_joins_writer_releases_response_and_removes_file(runtime):
    import threading
    from dovideo.infrastructure.r4_media import R4MediaPipeline
    pipeline, _, counts = runtime
    request = await register(pipeline, AnalysisRequest(MediaRef(10, "minio://media/a"), "A"))
    began, finish = threading.Event(), threading.Event()
    class Response:
        closed = released = False
        reads = 0
        def read(self, size):
            self.reads += 1
            if self.reads > 1:
                return b""
            began.set()
            assert finish.wait(5)
            return VIDEO_BYTES
        def close(self): self.closed = True
        def release_conn(self): self.released = True
    response = Response()
    pipeline.infrastructure.minio_client = SimpleNamespace(get_object=lambda *args: response)
    pipeline._download = R4MediaPipeline._download.__get__(pipeline)
    task = asyncio.create_task(pipeline.build_context(request))
    try:
        assert await asyncio.to_thread(began.wait, 5)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert response.closed and response.released and counts["asr_ocr"] == 0
    assert not list(pipeline.infrastructure.settings.media_workspace.glob("r4-inputs/*"))
    from dovideo.application.content_context import ContentContextKey
    assert await pipeline.context_preparation.artifacts.load(ContentContextKey(VIDEO_MD5)) is None


@pytest.mark.asyncio
async def test_existing_legacy_checkpoint_cannot_bypass_md5_provenance(runtime):
    from dovideo.application.content_context import ContentContextKey
    from dovideo.application import VideoContextBuilder, AsrBranchOutcome, OcrBranchOutcome, MediaObservationBundle, TranscriptSpan
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    request = AnalysisRequest(MediaRef(10, "minio://media/a"), "goal")
    await register(pipeline, request)
    legacy_identity = "sha256:" + hashlib.sha256(VIDEO_BYTES).hexdigest() + ":pipeline:legacy"
    first = VideoContextBuilder().build(request.media.source, request.goal,
        MediaObservationBundle(AsrBranchOutcome((TranscriptSpan(0, 5000, "Opening"),), attempted=1), OcrBranchOutcome()),
        media_content_identity=legacy_identity)
    await checkpoint.save_context(10, first)
    await checkpoint.save_chunks(10, (VideoChunk(start_ms=0, end_ms=60000, source_revision=first.source_revision),))
    second = await load(boundary, request)
    assert counts["asr_ocr"] == 1 and first.source_revision != second.source_revision
    assert await checkpoint.load_chunks(10) == ()
    assert await pipeline.context_preparation.artifacts.load(ContentContextKey(VIDEO_MD5)) is not None
    assert not EvidenceVerificationService().supported(second, AnalysisEvidence(
        timestamp_ms=1000, source="ASR", content="Opening", claim="Opening",
        source_revision=first.source_revision, segment_id=first.segments[0].segment_id,
        source_item_ids=(first.segments[0].asr_source_items[0].source_item_id,),
        source_provenance_version=first.provenance_version))
    # An unfinished old execution is immutable history. The existing guard
    # refuses to hydrate it into the new source revision.
    from dovideo.application import ExecutionRecordService
    from dovideo.application.execution_record import InMemoryExecutionRecordRepository, ExecutionRecordConflictError
    records = ExecutionRecordService(InMemoryExecutionRecordRepository())
    old = await records.start_or_resume(request.task_key, media_identity=request.media.source, source_revision=first.source_revision)
    with pytest.raises(ExecutionRecordConflictError, match="source revision mismatch"):
        await records.start_or_resume(request.task_key, media_identity=request.media.source, source_revision=second.source_revision)
    assert (await records.load_for_task(request.task_key)).execution_id == old.execution_id


@pytest.mark.asyncio
async def test_reused_context_tasks_keep_independent_agent_results_and_execution_records(runtime):
    from dovideo.application import TaskWorker, ExecutionRecordService
    from dovideo.application.execution_record import InMemoryExecutionRecordRepository
    from dovideo.infrastructure.persistence.task_lifecycle import CheckpointTaskLifecycleStore
    from dovideo.domain import AgentState, AnalysisResult, AnalysisMode
    from dovideo.application import WorkerDisposition

    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    records = ExecutionRecordService(InMemoryExecutionRecordRepository())
    class TaskLock(Lock):
        lease_seconds = None
    class Active:
        async def refresh(self, *args, **kwargs): pass
        async def release(self, *args): pass
    class Agent:
        def __init__(self): self.calls = []
        async def run(self, context, media_id=None, **kwargs):
            self.calls.append((media_id, context.source))
            return AgentState(goal=context.user_goal, result=AnalysisResult(title=f"Task {len(self.calls)}", conclusions=("Opening",)))
    agent = Agent()
    worker = TaskWorker(TaskLock(), Active(), CheckpointTaskLifecycleStore(checkpoint.repository),
                        boundary, agent, checkpoint, execution_records=records)
    a = MediaRef(10, "minio://media/user-A/video")
    b = MediaRef(57, "minio://media/user-B/video")
    requests = [AnalysisRequest(a, "生成复习笔记"), AnalysisRequest(b, "生成复习笔记"),
                AnalysisRequest(a, "提取操作步骤"), AnalysisRequest(a, "生成复习笔记", AnalysisMode.LEARNING)]
    for request in requests[:2]:
        await register(pipeline, request)
    for request in requests:
        token = bind_r4_request(request)
        try:
            outcome = await worker.handle(request)
            assert outcome.disposition is WorkerDisposition.COMPLETED
        finally:
            reset_r4_request(token)
    assert counts == {"download": 1, "asr_ocr": 1} and len(agent.calls) == 4
    executions = [await records.load_for_task(request.task_key) for request in requests]
    a, b = executions[:2]
    assert len({record.execution_id for record in executions}) == 4
    results = [await checkpoint.load_result(request.task_key) for request in requests]
    assert len({result.result.title for result in results}) == 4
    assert a.execution_id != b.execution_id and a.task_key.media_id != b.task_key.media_id
    assert a.media_identity == requests[0].media.source and b.media_identity == requests[1].media.source
    assert (await checkpoint.load_result(requests[0].task_key)).result.title != (await checkpoint.load_result(requests[1].task_key)).result.title

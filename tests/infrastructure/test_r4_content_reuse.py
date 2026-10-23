import asyncio
import hashlib
from types import SimpleNamespace

import pytest

from dovideo.application import AgentCheckpointService, AnalysisRequest, MediaRef
from dovideo.application.content_context import ContentContextPreparation
from dovideo.infrastructure.persistence.content_context import SqlAlchemyContentArtifacts
from dovideo.infrastructure.persistence.repository import CheckpointRepository
from dovideo.infrastructure.persistence.sqlalchemy import SqlAlchemyCheckpointStore, create_schema, create_sqlalchemy_engine
from dovideo.infrastructure.r4_runtime import R4MediaPipeline, R4RequestContextCheckpoint, bind_r4_request, reset_r4_request
from dovideo.presentation.composition import AnalysisSettings
from dovideo.domain import AnalysisEvidence, VideoChunk
from dovideo.application import EvidenceVerificationService


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
        settings=SimpleNamespace(media_workspace=tmp_path, minio_bucket="media"))
    events = SimpleNamespace(publish=lambda *args: asyncio.sleep(0))
    pipeline = R4MediaPipeline(infrastructure, AnalysisSettings(), telemetry, events)
    pipeline.context_preparation = ContentContextPreparation(SqlAlchemyContentArtifacts(engine), Lock(), poll_seconds=.005)
    counts = {"download": 0, "asr_ocr": 0}

    async def download(request):
        counts["download"] += 1
        path = tmp_path / f"download-{counts['download']}.mp4"
        path.write_bytes(b"identical target media bytes")
        return path
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
    context=await load(boundary,request)
    prepare.assert_awaited_once_with(10,context)
    assert (await checkpoint.load_context(10)).source_revision==context.source_revision


@pytest.mark.asyncio
async def test_actual_r4_pipeline_cross_media_rebinding_and_evidence(runtime):
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    video_bytes = b"identical target media bytes"
    legacy_md5 = hashlib.md5(video_bytes).hexdigest()
    sha256 = hashlib.sha256(video_bytes).hexdigest()
    a = AnalysisRequest(MediaRef(10, "minio://media/user-A/video", content_hash=legacy_md5), "same goal")
    b = AnalysisRequest(MediaRef(57, "minio://media/user-B/video", content_hash=sha256), "same goal")
    first = await load(boundary, a)
    second = await load(boundary, b)
    revised = await load(boundary, AnalysisRequest(a.media, "other goal"))
    assert counts == {"download": 3, "asr_ocr": 1}
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
    assert (await checkpoint.load_context(57)).source == b.media.source
    assert not list(pipeline.infrastructure.settings.media_workspace.glob("download-*.mp4"))


@pytest.mark.asyncio
async def test_actual_r4_different_bytes_never_reuse_matching_stored_hash(runtime):
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    stored_hash = hashlib.sha256(b"identical target media bytes").hexdigest()
    first_request = AnalysisRequest(
        MediaRef(10, "minio://media/user-A/video", content_hash=stored_hash), "goal"
    )
    first = await load(boundary, first_request)

    async def different_download(request):
        counts["download"] += 1
        path = pipeline.infrastructure.settings.media_workspace / "different-video.mp4"
        path.write_bytes(b"different target media bytes")
        return path

    pipeline._download = different_download
    second_request = AnalysisRequest(
        MediaRef(57, "minio://media/user-B/video", content_hash=stored_hash), "goal"
    )
    second = await load(boundary, second_request)

    assert counts == {"download": 2, "asr_ocr": 2}
    assert first.source_revision != second.source_revision
    assert first.segments != second.segments
    assert second.source == second_request.media.source
    assert (await checkpoint.load_context(10)).source_revision == first.source_revision
    assert (await checkpoint.load_context(57)).source_revision == second.source_revision
    assert not list(pipeline.infrastructure.settings.media_workspace.glob("*.mp4"))


@pytest.mark.asyncio
async def test_actual_r4_concurrent_different_media_build_once(runtime):
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    a = AnalysisRequest(MediaRef(10, "minio://media/a"), "a")
    b = AnalysisRequest(MediaRef(57, "minio://media/b"), "b")
    first, second = await asyncio.gather(load(boundary, a), load(boundary, b))
    assert counts["asr_ocr"] == 1 and first.source != second.source


@pytest.mark.asyncio
async def test_existing_media_checkpoint_cannot_bypass_changed_pipeline(runtime):
    pipeline, checkpoint, counts = runtime
    boundary = R4RequestContextCheckpoint(checkpoint, pipeline)
    request = AnalysisRequest(MediaRef(10, "minio://media/a"), "goal")
    first = await load(boundary, request)
    await checkpoint.save_chunks(10, (VideoChunk(start_ms=0, end_ms=60000, source_revision=first.source_revision),))
    pipeline.pipeline_contract = "changed-contract-v2"
    second = await load(boundary, request)
    assert counts["asr_ocr"] == 2 and first.source_revision != second.source_revision
    assert await checkpoint.load_chunks(10) == ()


@pytest.mark.asyncio
async def test_reused_context_tasks_keep_independent_agent_results_and_execution_records(runtime):
    from dovideo.application import TaskWorker, ExecutionRecordService
    from dovideo.application.execution_record import InMemoryExecutionRecordRepository
    from dovideo.infrastructure.persistence.task_lifecycle import CheckpointTaskLifecycleStore
    from dovideo.domain import AgentState, AnalysisResult
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
            return AgentState(goal=context.user_goal, result=AnalysisResult(title=f"Task {media_id}", conclusions=("Opening",)))
    agent = Agent()
    worker = TaskWorker(TaskLock(), Active(), CheckpointTaskLifecycleStore(checkpoint.repository),
                        boundary, agent, checkpoint, execution_records=records)
    requests = [AnalysisRequest(MediaRef(10, "minio://media/user-A/video"), "goal"),
                AnalysisRequest(MediaRef(57, "minio://media/user-B/video"), "goal")]
    for request in requests:
        token = bind_r4_request(request)
        try:
            outcome = await worker.handle(request)
            assert outcome.disposition is WorkerDisposition.COMPLETED
        finally:
            reset_r4_request(token)
    assert counts["asr_ocr"] == 1 and len(agent.calls) == 2
    a, b = [await records.load_for_task(request.task_key) for request in requests]
    assert a.execution_id != b.execution_id and a.task_key.media_id != b.task_key.media_id
    assert a.media_identity == requests[0].media.source and b.media_identity == requests[1].media.source
    assert (await checkpoint.load_result(requests[0].task_key)).result.title != (await checkpoint.load_result(requests[1].task_key)).result.title

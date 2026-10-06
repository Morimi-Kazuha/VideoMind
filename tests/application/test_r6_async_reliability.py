"""R6 deterministic execution, revision and reconnect fault injection."""
from dataclasses import replace

import pytest

from dovideo.application import (
    AgentCheckpointService, AnalysisRequest, AnalysisStatusQuery, DispatchDisposition,
    MediaRef, TaskDispatchService, TaskKey, TaskLifecycle, TaskWorker, WorkerDisposition,
)
from dovideo.application.status_projection import TaskStatusProjection
from dovideo.application.task_lifecycle import TaskLifecycleEvent
from dovideo.domain import (
    AgentPlan, AgentState, AnalysisEvidence, AnalysisMode, AnalysisResult, CriticResult, TaskEvent,
    TaskStage, TaskStatus, TaskStatusState, VideoContext, VideoSegment,
)
from dovideo.infrastructure.celery_runtime import R3StatusCheckpoint
from dovideo.infrastructure.persistence import CheckpointRepository, InMemoryHotCheckpointCache, SqliteCheckpointStore
from dovideo.infrastructure.redis import RedisTaskActiveMarker, RedisTaskCompletionMarker, RedisTaskLock
from dovideo.presentation.api.r4_runtime import ProductionR4Services


class Activity:
    def __init__(self): self.keys = set()
    async def reserve(self, key, **kwargs):
        if key in self.keys: return False
        self.keys.add(key)
        return True
    async def is_active(self, key): return key in self.keys
    async def release(self, key): self.keys.discard(key)
    async def refresh(self, key, **kwargs): self.keys.add(key)


class Lifecycles:
    def __init__(self): self.values = {}
    async def save_lifecycle(self, value): self.values[value.key] = value
    async def load_lifecycle(self, key): return self.values.get(key)


class Lock:
    lease_seconds = None

    async def refresh(self, key, token):
        return True

    async def acquire(self, key): return object()
    async def release(self, key, token): pass


class Completion:
    def __init__(self): self.keys = set()
    async def is_completed(self, key): return key in self.keys
    async def mark_completed(self, key, **kwargs): self.keys.add(key)
    async def clear_completed(self, key): self.keys.discard(key)


def checkpoint():
    hot = InMemoryHotCheckpointCache()
    repo = CheckpointRepository(SqliteCheckpointStore(), hot)
    return AgentCheckpointService(repo), hot


def state(title='old'):
    return AgentState(goal='goal', result=AnalysisResult(title=title, conclusions=['verified']), critique=CriticResult(passed=True))


def request(media_id=7, request_id=None):
    return AnalysisRequest(MediaRef(media_id, f'memory://{media_id}', content_hash='a' * 32), 'goal', request_id=request_id)


@pytest.mark.asyncio
async def test_same_content_different_media_dispatch_and_result_identities_are_independent():
    active, life = Activity(), Lifecycles()
    cp, _ = checkpoint()
    dispatch = TaskDispatchService(active, lifecycle=life)
    a, b = request(7), request(8)
    assert a.media.content_hash == b.media.content_hash
    assert await dispatch.dispatch(a) is DispatchDisposition.ACCEPTED
    assert await dispatch.dispatch(b) is DispatchDisposition.ACCEPTED
    assert await dispatch.dispatch(a) is DispatchDisposition.DUPLICATE
    for item, title in [(a, 'A'), (b, 'B')]:
        await cp.save_result(item.task_key, state(title))
        assert await life.load_lifecycle(item.task_key) is not None
    assert (await cp.load_result(a.task_key)).result.title == 'A'
    assert (await cp.load_result(b.task_key)).result.title == 'B'
    for adapter in [RedisTaskActiveMarker(None), RedisTaskCompletionMarker(None), RedisTaskLock(None)]:
        assert adapter.redis_key(a.task_key) != adapter.redis_key(b.task_key)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', [TaskStage.QUEUED, TaskStage.CRITIC_STARTED])
async def test_active_revision_masks_old_result(stage):
    cp, _ = checkpoint()
    active, life = Activity(), Lifecycles()
    item = request(request_id='revision:new')
    await cp.save_result(item.task_key, state())
    current = TaskLifecycle.new(item.task_key, request_id=item.request_id).queued()
    if stage is not TaskStage.QUEUED: current = current.begin_attempt(stage=stage)
    await life.save_lifecycle(current)
    await active.reserve(item.task_key)
    query = AnalysisStatusQuery(R3StatusCheckpoint(cp, life), active)
    status = await query.current(7, 'goal')
    assert status.state is (TaskStatusState.QUEUED if stage is TaskStage.QUEUED else TaskStatusState.PROCESSING)
    assert (await cp.load_result(item.task_key)).result.title == 'old'


@pytest.mark.asyncio
async def test_failed_revision_enqueue_preserves_previous_result_and_releases_reservation():
    cp, _ = checkpoint()
    active, life = Activity(), Lifecycles()
    item = request(request_id='revision:new')
    await cp.save_result(item.task_key, state())
    class Broker:
        async def enqueue(self, item): raise RuntimeError('broker unavailable')
    dispatch = TaskDispatchService(active, lifecycle=life, transport=Broker())
    assert await dispatch.dispatch(item, revision_plan=AgentPlan(understoodGoal='goal', tasks=['new']), revision_checkpoint=cp) is DispatchDisposition.FAILED
    assert not await active.is_active(item.task_key)
    assert not await cp.begin_staged_revision(7, 'goal', item.mode)
    assert (await AnalysisStatusQuery(R3StatusCheckpoint(cp, life), active).current(7, 'goal')).state is TaskStatusState.COMPLETED
    assert (await cp.load_result(item.task_key)).result.title == 'old'


@pytest.mark.asyncio
async def test_revision_uses_original_task_key_and_replaces_current_result_once():
    cp, _ = checkpoint()
    active, life, completion = Activity(), Lifecycles(), Completion()
    item = request(request_id='revision:new')
    await cp.save_result(item.task_key, state())
    await cp.save_context(7, VideoContext(source='memory://7', user_goal='goal', segments=[VideoSegment(start_ms=0, end_ms=1000, transcript='verified')]))
    completion.keys.add(item.task_key)
    dispatch = TaskDispatchService(active, lifecycle=life, completion=completion)
    revised_plan = AgentPlan(understoodGoal='goal', tasks=['new'])
    assert await dispatch.dispatch(item, revision_plan=revised_plan, revision_checkpoint=cp) is DispatchDisposition.ACCEPTED
    assert await dispatch.dispatch(replace(item, request_id='revision:conflict'), revision_plan=AgentPlan(understoodGoal='goal', tasks=['bad']), revision_checkpoint=cp) is DispatchDisposition.DUPLICATE
    class Loop:
        calls = 0
        async def run(self, context, media_id=None, profile=None):
            self.calls += 1
            assert await cp.load_plan(item.task_key) == revised_plan
            return state('new')
    loop = Loop()
    worker = TaskWorker(Lock(), active, life, cp, loop, cp, completion=completion)
    # Old delivery cannot consume the newly staged plan or release its lease.
    assert (await worker.handle(request())).disposition is WorkerDisposition.STALE
    assert await active.is_active(item.task_key)
    assert (await cp.load_result(item.task_key)).result.title == 'old'
    assert (await worker.handle(item)).disposition is WorkerDisposition.COMPLETED
    assert (await worker.handle(item)).disposition is WorkerDisposition.COMPLETED
    assert loop.calls == 1
    assert (await cp.load_result(item.task_key)).result.title == 'new'
    assert (await AnalysisStatusQuery(R3StatusCheckpoint(cp, life), active).current(7, 'goal')).state is TaskStatusState.COMPLETED


@pytest.mark.asyncio
async def test_old_planner_hydration_cannot_regress_current_critic_stage():
    cp, hot = checkpoint()
    key = request().task_key
    await cp.save_plan(key, AgentPlan(understoodGoal='goal', tasks=['original']))
    await cp.save_stage(key, TaskStage.CRITIC_STARTED)
    redis_key = cp.goal_key(7, 'goal')
    hot.delete_hash(redis_key, 'plan')
    assert (await cp.load_plan(key)).tasks == ('original',)
    assert await cp.load_stage(key) is TaskStage.CRITIC_STARTED
    assert hot.get_hash(redis_key, 'stage') == TaskStage.CRITIC_STARTED.value


def test_terminal_projection_rejects_late_nonterminal_and_old_attempt_terminal():
    key = request().task_key
    projection = TaskStatusProjection()
    def event(stage, status, attempt):
        return TaskLifecycleEvent(key, TaskEvent.of(TaskStatus.of(status, 'status'), stage), attempt=attempt)
    projection.apply(event(TaskStage.CRITIC_STARTED, TaskStatusState.PROCESSING, 2))
    projection.apply(event(TaskStage.FAILED, TaskStatusState.FAILED, 1))
    assert projection.stage(key) is TaskStage.CRITIC_STARTED
    projection.apply(event(TaskStage.COMPLETED, TaskStatusState.COMPLETED, 2))
    projection.apply(event(TaskStage.PLAN_COMPLETED, TaskStatusState.PROCESSING, 2))
    assert projection.current(key).state is TaskStatusState.COMPLETED


@pytest.mark.asyncio
async def test_sse_reconnect_snapshot_filters_old_execution_history_and_older_stages():
    cp, _ = checkpoint()
    active, life = Activity(), Lifecycles()
    item = request(request_id='revision:new')
    current = TaskLifecycle.new(item.task_key, request_id=item.request_id).queued().begin_attempt(stage=TaskStage.CRITIC_STARTED)
    await life.save_lifecycle(current)
    await active.reserve(item.task_key)
    values = [
        TaskLifecycleEvent(item.task_key, TaskEvent.of(TaskStatus.of(TaskStatusState.COMPLETED, 'old'), TaskStage.COMPLETED), attempt=1, request_id='revision:old'),
        TaskLifecycleEvent(item.task_key, TaskEvent.of(TaskStatus.of(TaskStatusState.PROCESSING, 'old planner'), TaskStage.PLAN_COMPLETED), attempt=1, request_id=item.request_id),
        TaskLifecycleEvent(item.task_key, TaskEvent.of(TaskStatus.of(TaskStatusState.COMPLETED, 'new done'), TaskStage.COMPLETED), attempt=1, request_id=item.request_id),
    ]
    class Events:
        async def read(self, key): return values
    services = object.__new__(ProductionR4Services)
    services.lifecycle = life
    services.status_query = AnalysisStatusQuery(R3StatusCheckpoint(cp, life), active)
    services.events = Events()
    stream = services.subscribe(item.task_key)
    assert (await anext(stream)).stage is TaskStage.CRITIC_STARTED
    assert (await anext(stream)).state is TaskStatusState.COMPLETED
    with pytest.raises(StopAsyncIteration): await anext(stream)


@pytest.mark.asyncio
async def test_sse_initial_snapshot_rechecks_execution_changed_during_status_read():
    from dovideo.application.durable_task_events import durable_task_events
    item = request(request_id='revision:new')
    life = Lifecycles()
    await life.save_lifecycle(TaskLifecycle.new(item.task_key, request_id='old').queued())
    class Query:
        calls = 0
        async def current(self, media_id, goal, mode):
            self.calls += 1
            await life.save_lifecycle(TaskLifecycle.new(item.task_key, request_id=item.request_id).queued())
            return TaskStatus.of(TaskStatusState.QUEUED, 'new execution')
    query = Query()
    stream = durable_task_events(item.task_key, life, query, None)
    initial = await anext(stream)
    assert initial.request_id == item.request_id
    assert query.calls == 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_same_media_reuse_preserves_provenance_and_other_media_has_no_final_result():
    cp, hot = checkpoint()
    context = VideoContext(source='memory://7', source_revision='original-revision', segments=[
        VideoSegment(start_ms=0, end_ms=1000, transcript='verified', segment_id='original-segment', source_revision='original-revision', evidence_frames=['frames/original.jpg'])
    ])
    evidence = AnalysisEvidence(timestamp_ms=0, source='ASR', content='verified', claim='verified', source_revision='original-revision', segment_id='original-segment', source_item_ids=['original-item'])
    original = AgentState(goal='goal', result=AnalysisResult(title='original', conclusions=['verified'], evidence=[evidence]))
    await cp.save_context(7, context)
    await cp.save_result(request().task_key, original)
    hot.delete_hash(cp.checkpoint_key(7))
    hot.delete_hash(cp.goal_key(7, 'goal'))
    reused_context = await cp.load_context(7)
    reused = await cp.load_result(request().task_key)
    assert reused_context.segments == context.segments
    assert reused_context.source_revision == context.source_revision
    assert reused.result.evidence == original.result.evidence
    assert await cp.load_result(request(8).task_key) is None


@pytest.mark.asyncio
async def test_revision_starts_new_execution_record_without_altering_prior_history():
    from dovideo.application.execution_record import ExecutionRecordService, InMemoryExecutionRecordRepository
    cp, _ = checkpoint()
    active, life = Activity(), Lifecycles()
    item = request(request_id='revision:history')
    context = VideoContext(source='memory://7', source_revision='revision-source', segments=[VideoSegment(start_ms=0, end_ms=1000, transcript='verified')])
    await cp.save_context(7, context)
    await cp.save_result(item.task_key, state())
    records = ExecutionRecordService(InMemoryExecutionRecordRepository())
    old = await records.start_or_resume(item.task_key, media_identity='memory://7', source_revision='revision-source')
    old = await records.complete(old.execution_id, state())
    await TaskDispatchService(active, lifecycle=life).dispatch(item, revision_plan=AgentPlan(understoodGoal='goal', tasks=['new']), revision_checkpoint=cp)
    class Loop:
        async def run(self, context, media_id=None, profile=None): return state('new')
    worker = TaskWorker(Lock(), active, life, cp, Loop(), cp, execution_records=records)
    outcome = await worker.handle(item)
    assert outcome.disposition is WorkerDisposition.COMPLETED
    latest = await records.load_for_task(item.task_key)
    assert latest.execution_id != old.execution_id
    assert latest.request_id == item.request_id
    assert await records.load(old.execution_id) == old


@pytest.mark.asyncio
async def test_local_retranscription_old_terminal_does_not_mask_new_active_task(tmp_path):
    from dovideo.presentation.api.runtime import LocalR1Services
    services = LocalR1Services(work_dir=tmp_path)
    record = await services.ingest(11, 'local.mp4', b'fixture', 'video/mp4')
    await services.start_transcription(record.media_id, 11)
    await services.transcription_tasks[record.media_id]
    assert (await services.transcription_status(record.media_id, 11)).state is TaskStatusState.COMPLETED
    await services.start_transcription(record.media_id, 11)
    assert (await services.transcription_status(record.media_id, 11)).state is TaskStatusState.QUEUED
    await services.transcription_tasks[record.media_id]
    assert (await services.transcription_status(record.media_id, 11)).state is TaskStatusState.COMPLETED


@pytest.mark.asyncio
async def test_orphaned_local_processing_transcription_becomes_retryable_failure(tmp_path):
    from dovideo.presentation.api.runtime import LocalR1Services
    services = LocalR1Services(work_dir=tmp_path)
    record = await services.ingest(11, 'local.mp4', b'fixture', 'video/mp4')
    key = TaskKey(record.media_id, '__transcription__')
    await services.lifecycle.save_lifecycle(TaskLifecycle.new(key).queued().begin_attempt(stage=TaskStage.TRANSCRIPTION))
    status = await services.transcription_status(record.media_id, 11)
    assert status.state is TaskStatusState.FAILED
    assert '重新提交' in status.message


@pytest.mark.asyncio
async def test_orphaned_revision_does_not_relabel_old_answer_as_new_completion():
    cp, _ = checkpoint()
    life, active = Lifecycles(), Activity()
    item = request(request_id='revision:orphan')
    await cp.save_result(item.task_key, state())
    await life.save_lifecycle(TaskLifecycle.new(item.task_key, request_id=item.request_id).queued())
    status = await AnalysisStatusQuery(R3StatusCheckpoint(cp, life), active).current(7, 'goal')
    assert status.state is TaskStatusState.FAILED
    assert (await cp.load_result(item.task_key)).result.title == 'old'


@pytest.mark.asyncio
async def test_partial_revision_stage_and_failed_cleanup_cannot_be_consumed_by_old_execution():
    cp, _ = checkpoint()
    class FaultyCheckpoint(AgentCheckpointService):
        async def stage_revision(self, *args, **kwargs):
            await super().stage_revision(*args, **kwargs)
            raise OSError('stage write response lost')
        async def cancel_staged_revision(self, *args, **kwargs):
            raise OSError('cleanup unavailable')
    faulty = FaultyCheckpoint(cp.repository)
    active, life = Activity(), Lifecycles()
    old = request(request_id='revision:old')
    await cp.save_result(old.task_key, state())
    await life.save_lifecycle(TaskLifecycle.new(old.task_key, request_id=old.request_id).queued().begin_attempt().complete('old'))
    new = replace(old, request_id='revision:new')
    outcome = await TaskDispatchService(active, lifecycle=life).dispatch(new, revision_plan=AgentPlan(understoodGoal='goal', tasks=['new']), revision_checkpoint=faulty)
    assert outcome is DispatchDisposition.FAILED
    assert not await active.is_active(old.task_key)
    assert not await cp.begin_staged_revision(7, 'goal', old.mode, request_id=old.request_id)
    worker = TaskWorker(Lock(), active, life, cp, None, cp)
    assert (await worker.handle(old)).disposition is WorkerDisposition.COMPLETED
    assert (await cp.load_result(old.task_key)).result.title == 'old'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['before_apply', 'after_apply'])
async def test_revision_application_failure_has_bounded_retry_and_preserves_correct_plan(fault):
    cp, _ = checkpoint()
    class FaultyCheckpoint(AgentCheckpointService):
        calls = 0
        async def begin_staged_revision(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                if fault == 'after_apply':
                    await super().begin_staged_revision(*args, **kwargs)
                raise OSError('store temporarily unavailable')
            return await super().begin_staged_revision(*args, **kwargs)
    faulty = FaultyCheckpoint(cp.repository)
    active, life = Activity(), Lifecycles()
    item = request(request_id='revision:retry')
    await cp.save_result(item.task_key, state())
    await cp.save_context(7, VideoContext(source='memory://7', user_goal='goal', segments=[VideoSegment(start_ms=0, end_ms=1000, transcript='verified')]))
    plan = AgentPlan(understoodGoal='goal', tasks=['revised'])
    await TaskDispatchService(active, lifecycle=life).dispatch(item, revision_plan=plan, revision_checkpoint=faulty)
    class Loop:
        calls = 0
        async def run(self, context, **kwargs):
            self.calls += 1
            assert await cp.load_plan(item.task_key) == plan
            return state('new')
    loop = Loop()
    worker = TaskWorker(Lock(), active, life, faulty, loop, faulty)
    first = await worker.handle(item)
    assert first.disposition is WorkerDisposition.RETRY
    assert first.attempt == 1
    assert await active.is_active(item.task_key)
    second = await worker.handle(item)
    assert second.disposition is WorkerDisposition.COMPLETED
    assert second.attempt == 2
    assert loop.calls == 1
    assert (await cp.load_result(item.task_key)).result.title == 'new'


@pytest.mark.asyncio
async def test_legitimate_worker_retry_keeps_request_identity_and_rebinds_shared_context_goal():
    cp, _ = checkpoint()
    active, life = Activity(), Lifecycles()
    item = request(request_id='same-execution')
    original = VideoContext(source='memory://7', user_goal='another goal', segments=[VideoSegment(start_ms=0, end_ms=1000, transcript='verified')])
    await cp.save_context(7, original)
    stored_context = await cp.load_context(7)
    await TaskDispatchService(active, lifecycle=life).dispatch(item)
    class Loop:
        calls = 0
        async def run(self, context, **kwargs):
            self.calls += 1
            assert context.user_goal == item.goal
            if self.calls == 1: raise OSError('retryable')
            return state('new')
    worker = TaskWorker(Lock(), active, life, cp, Loop(), cp)
    assert (await worker.handle(item)).disposition is WorkerDisposition.RETRY
    assert (await worker.handle(replace(item, request_id='stale'))).disposition is WorkerDisposition.STALE
    assert await active.is_active(item.task_key)
    final = await worker.handle(item)
    assert final.disposition is WorkerDisposition.COMPLETED
    assert final.attempt == 2
    assert await cp.load_context(7) == stored_context
    assert original.user_goal == 'another goal'


@pytest.mark.asyncio
async def test_sse_attempt_change_during_snapshot_rejects_previous_attempt_terminal():
    from dovideo.application.durable_task_events import durable_task_events
    item = request(request_id='same-execution')
    life = Lifecycles()
    old = TaskLifecycle.new(item.task_key, request_id=item.request_id).queued().begin_attempt(stage=TaskStage.CRITIC_STARTED).retry()
    await life.save_lifecycle(old)
    class Query:
        calls = 0
        async def current(self, *args):
            self.calls += 1
            if self.calls == 1: await life.save_lifecycle(old.begin_attempt())
            return (await life.load_lifecycle(item.task_key)).status
    class Events:
        async def read(self, key):
            return [
                TaskLifecycleEvent(key, TaskEvent.of(TaskStatus.of(TaskStatusState.FAILED, 'old failure'), TaskStage.FAILED), attempt=1, request_id=item.request_id),
                TaskLifecycleEvent(key, TaskEvent.of(TaskStatus.of(TaskStatusState.COMPLETED, 'new success'), TaskStage.COMPLETED), attempt=2, request_id=item.request_id),
            ]
    stream = durable_task_events(item.task_key, life, Query(), Events())
    assert (await anext(stream)).attempt == 2
    final = await anext(stream)
    assert final.state is TaskStatusState.COMPLETED
    assert final.event.message == 'new success'
    await stream.aclose()


@pytest.mark.asyncio
async def test_failed_retranscription_retains_last_successful_text_with_failed_status(tmp_path):
    from dovideo.presentation.api.runtime import LocalR1Services
    services = LocalR1Services(work_dir=tmp_path)
    record = await services.ingest(11, 'local.mp4', b'fixture', 'video/mp4')
    await services.start_transcription(record.media_id, 11)
    await services.transcription_tasks[record.media_id]
    old = (await services.transcription_status(record.media_id, 11)).result
    services.contexts.pop(record.media_id)
    await services.start_transcription(record.media_id, 11)
    assert (await services.transcription_status(record.media_id, 11)).state is TaskStatusState.QUEUED
    await services.transcription_tasks[record.media_id]
    status = await services.transcription_status(record.media_id, 11)
    assert status.state is TaskStatusState.FAILED
    assert status.result == old
    stream = services.subscribe(TaskKey(record.media_id, '__transcription__'))
    initial = await anext(stream)
    assert initial.state is TaskStatusState.FAILED
    assert initial.event.result == old
    await stream.aclose()


@pytest.mark.asyncio
async def test_local_subscriber_is_removed_when_initial_status_read_fails(tmp_path):
    from dovideo.presentation.api.runtime import LocalR1Services
    services = LocalR1Services(work_dir=tmp_path)
    failure = RuntimeError('status unavailable')
    async def failed(*args): raise failure
    services.status_query.current = failed
    key = request().task_key
    with pytest.raises(RuntimeError) as error: await anext(services.subscribe(key))
    assert error.value is failure
    assert key not in services.subscribers

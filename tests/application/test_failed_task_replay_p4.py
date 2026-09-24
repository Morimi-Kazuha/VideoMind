from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from dovideo.application import (
    AnalysisRequest,
    DispatchDisposition,
    FailedTaskAdminConflict,
    FailedTaskAdminService,
    MediaRef,
    TaskKey,
    TaskDispatchService,
    TaskLifecycle,
)
from dovideo.application.analysis_task_keys import normalize_content_hash
from dovideo.domain import AnalysisMode, TaskStage, TaskStatusState
from dovideo.infrastructure.persistence.sqlalchemy import (
    FailedTaskRecord,
    SqlAlchemyFailedTaskStore,
    create_schema,
)


class FakeMedia:
    def __init__(self, media_id: int, *, exists: bool = True, user_id: int = 7) -> None:
        self.record = (
            SimpleNamespace(
                media_id=media_id,
                user_id=user_id,
                content_hash="a" * 32,
                filename="video.mp4",
                source="memory://video",
                status="READY",
            )
            if exists
            else None
        )

    async def get(self, media_id: int):
        return self.record if self.record is not None and self.record.media_id == media_id else None


class FakeLifecycle:
    def __init__(self) -> None:
        self.values = {}

    async def load_lifecycle(self, key):
        return self.values.get(key)

    async def save_lifecycle(self, lifecycle):
        self.values[lifecycle.key] = lifecycle


class FakeCheckpoint:
    def __init__(self) -> None:
        self.results = {}

    async def load_result(self, key):
        return self.results.get(key)


class FakeHandoff:
    def __init__(self) -> None:
        self.pending = False

    async def load_pending(self, _key):
        return object() if self.pending else None


class ExistingDispatch:
    """Route the replay callback through the real TaskDispatchService."""

    def __init__(self, lifecycle: FakeLifecycle) -> None:
        self.lifecycle = lifecycle
        self.calls = []
        self.active: set[TaskKey] = set()
        self.guard = asyncio.Lock()
        self.transport = _CaptureTransport()
        self.dispatcher = TaskDispatchService(
            _ActiveMarker(self.active, self.guard),
            lifecycle=lifecycle,
            transport=self.transport,
        )

    @property
    def enqueued(self):
        return [request.task_key for request in self.transport.requests]

    async def __call__(self, media_id, owner_id, goal, mode, *, request_id):
        key = TaskKey(media_id, goal, mode)
        self.calls.append((media_id, owner_id, goal, mode, request_id))
        return await self.dispatcher.dispatch(
            AnalysisRequest(
                MediaRef(
                    media_id=media_id,
                    source="memory://replay",
                    filename="video.mp4",
                    content_hash="a" * 32,
                    status="READY",
                ),
                goal,
                mode,
                request_id=request_id,
            )
        )


class _ActiveMarker:
    def __init__(self, active: set[TaskKey], guard: asyncio.Lock) -> None:
        self.active = active
        self.guard = guard

    async def reserve(self, key, *, ttl_seconds: float) -> bool:
        del ttl_seconds
        async with self.guard:
            if key in self.active:
                return False
            self.active.add(key)
            return True

    async def release(self, key) -> None:
        async with self.guard:
            self.active.discard(key)


class _CaptureTransport:
    def __init__(self) -> None:
        self.requests = []

    async def enqueue(self, request) -> None:
        self.requests.append(request)


def _make_service(tmp_path, *, mode="REVIEW", media_exists=True, action="START_ANALYSIS"):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'failed-task-replay.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    create_schema(engine)
    store = SqlAlchemyFailedTaskStore(engine)
    media_id = 41
    goal = "preserve the original goal"
    record = store.record(
        FailedTaskRecord(
            media_id=media_id,
            action=action,
            mode=mode,
            content_hash=normalize_content_hash(media_id, "a" * 32),
            user_goal=goal,
            attempt_count=3,
            error_type="ValueError",
            error_message="historical diagnostic",
            status="DEAD_LETTER_PENDING",
        )
    )
    lifecycle = FakeLifecycle()
    key = TaskKey(media_id, goal, AnalysisMode.REVIEW if mode == "REVIEW" else AnalysisMode.GENERAL)
    lifecycle.values[key] = (
        TaskLifecycle.new(key).queued().begin_attempt().fail(stage=TaskStage.DEAD_LETTERED)
    )
    checkpoint = FakeCheckpoint()
    dispatcher = ExistingDispatch(lifecycle)
    handoff = FakeHandoff()
    service = FailedTaskAdminService(
        store,
        FakeMedia(media_id, exists=media_exists),
        lifecycle,
        checkpoint,
        handoff,
        dispatcher,
    )
    return service, store, record, key, lifecycle, checkpoint, dispatcher, engine


@pytest.mark.asyncio
async def test_replay_preserves_task_key_and_uses_existing_dispatch_boundary(tmp_path):
    service, store, original, key, _lifecycle, _checkpoint, dispatcher, engine = _make_service(tmp_path)
    try:
        result = await service.replay(original.task_id, "operator-replay-key-0001")

        assert result.accepted is True
        assert dispatcher.enqueued == [key]
        media_id, owner_id, goal, mode, request_id = dispatcher.calls[0]
        assert (media_id, owner_id, goal, mode) == (
            key.media_id,
            7,
            key.goal,
            AnalysisMode.REVIEW,
        )
        assert request_id == result.view.replay_attempts[0].attempt_id
        assert key == TaskKey(media_id, goal, mode)
        assert result.view.record.user_goal == "preserve the original goal"
        assert result.view.record.error_message == "historical diagnostic"
        assert result.view.replay_status == "RUNNING"
        assert result.view.record.replay_attempt_count == 1
        assert store.list_replay_attempts(original.task_id)[0].status == "DISPATCHED"
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_same_key_is_idempotent_and_concurrent_requests_enqueue_once(tmp_path):
    service, _store, original, _key, _lifecycle, _checkpoint, dispatcher, engine = _make_service(tmp_path)
    try:
        first, second = await asyncio.gather(
            service.replay(original.task_id, "operator-replay-key-0002"),
            service.replay(original.task_id, "operator-replay-key-0002"),
        )
        assert first.accepted and second.accepted
        assert len(dispatcher.enqueued) == 1
        assert first.view.replay_attempts[0].attempt_id == second.view.replay_attempts[0].attempt_id

        calls_after_concurrent_pair = len(dispatcher.calls)
        repeated = await service.replay(original.task_id, "operator-replay-key-0002")
        assert repeated.accepted is True
        assert len(dispatcher.calls) == calls_after_concurrent_pair
        assert len(dispatcher.enqueued) == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_replay_rechecks_eligibility_and_preserves_history_after_refailure(tmp_path):
    service, store, original, key, lifecycle, checkpoint, dispatcher, engine = _make_service(tmp_path)
    try:
        first = await service.replay(original.task_id, "operator-replay-key-0003")
        attempt_id = first.view.replay_attempts[0].attempt_id
        lifecycle.values[key] = (
            TaskLifecycle.new(key, request_id=attempt_id)
            .queued()
            .begin_attempt()
            .fail(stage=TaskStage.DEAD_LETTERED)
        )

        detail = await service.inspect(original.task_id)
        assert detail.replay_status == "FAILED_AGAIN"
        assert detail.replay_eligible is True
        assert detail.record.error_message == "historical diagnostic"
        with pytest.raises(FailedTaskAdminConflict):
            await service.replay(original.task_id, "operator-replay-key-0003")

        dispatcher.active.discard(key)
        second = await service.replay(original.task_id, "operator-replay-key-0004")
        assert second.accepted is True
        assert second.view.record.replay_attempt_count == 2
        assert store.get_failed(original.task_id).error_type == "ValueError"
        assert checkpoint.results == {}  # no successful result was deleted or overwritten
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing-media", "原视频已不存在"),
        ("queued", "任务生命周期不是终态失败"),
        ("completed", "任务生命周期不是终态失败"),
        ("malformed-mode", "失败记录缺少可重建"),
        ("invalid-action", "不是可重放的分析任务"),
    ],
)
async def test_ineligible_tasks_are_conflicts_not_dispatched(tmp_path, case, expected):
    options = {}
    if case == "missing-media":
        options["media_exists"] = False
    if case == "malformed-mode":
        options["mode"] = "AUTO"
    if case == "invalid-action":
        options["action"] = "INVALID_TRANSPORT"
    service, _store, original, key, lifecycle, checkpoint, dispatcher, engine = _make_service(
        tmp_path,
        **options,
    )
    try:
        if case == "queued":
            lifecycle.values[key] = TaskLifecycle.new(key).queued()
        elif case == "completed":
            lifecycle.values[key] = TaskLifecycle.new(key).queued().begin_attempt().complete("ok")
            checkpoint.results[key] = object()
        with pytest.raises(FailedTaskAdminConflict, match=expected):
            await service.replay(original.task_id, "operator-replay-key-0005")
        assert dispatcher.enqueued == []
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_active_task_and_pending_handoff_are_not_replayed(tmp_path):
    service, _store, original, key, lifecycle, _checkpoint, dispatcher, engine = _make_service(tmp_path)
    try:
        lifecycle.values[key] = TaskLifecycle.new(key).queued()
        with pytest.raises(FailedTaskAdminConflict):
            await service.replay(original.task_id, "operator-replay-key-0006")

        lifecycle.values[key] = (
            TaskLifecycle.new(key).queued().begin_attempt().fail(stage=TaskStage.DEAD_LETTERED)
        )
        service._handoff.pending = True
        with pytest.raises(FailedTaskAdminConflict, match="DLQ handoff"):
            await service.replay(original.task_id, "operator-replay-key-0007")
        assert dispatcher.enqueued == []
    finally:
        engine.dispose()

"""Phase 9B-FIX2 durable dead-letter handoff tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from dovideo.application import (
    AnalysisRequest,
    MediaRef,
    PendingDeadLetterHandoff,
    TaskWorker,
    WorkerDisposition,
)
from dovideo.domain import TaskStage, TaskStatusState, VideoContext
from dovideo.infrastructure.persistence import (
    CheckpointDeadLetterHandoffStore,
    CheckpointRepository,
    InMemoryHotCheckpointCache,
    SqliteCheckpointStore,
)


def _request() -> AnalysisRequest:
    return AnalysisRequest(
        MediaRef(
            77,
            "memory://video",
            filename="video.mp4",
            content_hash="a" * 32,
            status="READY",
        ),
        "find the important evidence",
    )


class SharedLifecycle:
    def __init__(self) -> None:
        self.values = {}
        self.saves = []

    async def load_lifecycle(self, key):
        return self.values.get(key)

    async def save_lifecycle(self, lifecycle) -> None:
        self.values[lifecycle.key] = lifecycle
        self.saves.append(lifecycle)


class FailTerminalLifecycleOnce(SharedLifecycle):
    def __init__(self) -> None:
        super().__init__()
        self.fail_terminal_once = True

    async def save_lifecycle(self, lifecycle) -> None:
        if (
            self.fail_terminal_once
            and lifecycle.state is TaskStatusState.FAILED
            and lifecycle.stage is TaskStage.DEAD_LETTERED
        ):
            self.fail_terminal_once = False
            raise RuntimeError("injected terminal lifecycle persistence interruption")
        await super().save_lifecycle(lifecycle)


class SharedResults:
    def __init__(self) -> None:
        self.values = {}

    async def load_result(self, key):
        return self.values.get(key)

    async def save_result(self, key, state) -> None:
        self.values[key] = state


class SharedActive:
    def __init__(self) -> None:
        self.active = set()
        self.release_calls = []

    async def reserve(self, key, *, ttl_seconds: float) -> bool:
        if key in self.active:
            return False
        self.active.add(key)
        return True

    async def is_active(self, key) -> bool:
        return key in self.active

    async def refresh(self, key, *, ttl_seconds: float) -> None:
        self.active.add(key)

    async def release(self, key) -> None:
        self.release_calls.append(key)
        self.active.discard(key)


class Lock:
    def __init__(self) -> None:
        self.release_calls = []

    async def acquire(self, key):
        return object()

    async def release(self, key, token) -> None:
        self.release_calls.append((key, token))


class Context:
    def __init__(self) -> None:
        self.calls = []
        self.value = VideoContext(source="memory://video", user_goal="goal", segments=[])

    async def load_context(self, media_id: int):
        self.calls.append(media_id)
        return self.value


class Loop:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def run(self, context, media_id=None, profile=None):
        self.calls += 1
        raise self.outcome


class DeadLetter:
    def __init__(self, lifecycle: SharedLifecycle | None = None) -> None:
        self.calls = []
        self.fail_first = True
        self.lifecycle = lifecycle
        self.lifecycle_at_publish = []

    async def publish(self, request, *, attempt: int, error: BaseException) -> None:
        self.calls.append((request, attempt, error))
        if self.lifecycle is not None:
            self.lifecycle_at_publish.append(
                self.lifecycle.values.get(request.task_key)
            )
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("dead-letter broker unavailable")


class Events:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, key, event) -> None:
        self.events.append((key, event))


def _worker(
    request: AnalysisRequest,
    *,
    lifecycle: SharedLifecycle,
    results: SharedResults,
    active: SharedActive,
    handoff: CheckpointDeadLetterHandoffStore,
    dead_letter: DeadLetter,
    loop: Loop,
    context: Context,
    lock: Lock,
    events: Events,
) -> TaskWorker:
    return TaskWorker(
        lock,
        active,
        lifecycle,
        context,
        loop,
        results,
        events=events,
        dead_letter=dead_letter,
        dead_letter_handoff=handoff,
    )


@pytest.mark.asyncio
async def test_pending_handoff_is_json_safe_and_rebuilds_request() -> None:
    request = _request()
    original = ValueError("invalid analysis input")
    handoff = PendingDeadLetterHandoff.from_request(
        request,
        attempt=1,
        error=original,
    )

    assert handoff.task_key == request.task_key
    assert handoff.to_request() == request
    restored = handoff.to_exception()
    assert type(restored).__name__ == "PersistedDeadLetterError"
    assert restored.error_type == "ValueError"
    assert restored.error_message == "invalid analysis input"
    assert str(restored) == "ValueError: invalid analysis input"


@pytest.mark.asyncio
async def test_dead_letter_handoff_survives_worker_restart_and_retries_without_analysis(
    tmp_path: Path,
) -> None:
    request = _request()
    key = request.task_key
    lifecycle = SharedLifecycle()
    results = SharedResults()
    active = SharedActive()
    active.active.add(key)
    dead_letter = DeadLetter()

    database_path = tmp_path / "agent-checkpoints.sqlite3"
    durable_a = SqliteCheckpointStore(database_path)
    repository_a = CheckpointRepository(
        durable=durable_a,
        cache=InMemoryHotCheckpointCache(),
    )
    handoff_a = CheckpointDeadLetterHandoffStore(repository_a)
    context_a = Context()
    loop_a = Loop(ValueError("analysis invalid"))
    worker_a = _worker(
        request,
        lifecycle=lifecycle,
        results=results,
        active=active,
        handoff=handoff_a,
        dead_letter=dead_letter,
        loop=loop_a,
        context=context_a,
        lock=Lock(),
        events=Events(),
    )

    with pytest.raises(RuntimeError, match="dead-letter broker unavailable"):
        await worker_a.handle(request)

    failed = lifecycle.values[key]
    assert failed.state is TaskStatusState.FAILED
    assert failed.stage is TaskStage.DEAD_LETTERED
    assert failed.attempt == 1
    persisted = await handoff_a.load_pending(key)
    assert persisted is not None
    assert persisted.attempt == 1
    assert persisted.error_type == "ValueError"
    assert persisted.error_message == "analysis invalid"
    assert active.active == {key}
    assert loop_a.calls == 1
    assert context_a.calls == [77]
    assert len(dead_letter.calls) == 1
    durable_a.close()

    # Worker B has fresh process-local collaborators.  Only the external
    # lifecycle/active/result adapters and the reopened checkpoint database
    # are shared with Worker A.
    durable_b = SqliteCheckpointStore(database_path)
    repository_b = CheckpointRepository(
        durable=durable_b,
        cache=InMemoryHotCheckpointCache(),
    )
    handoff_b = CheckpointDeadLetterHandoffStore(repository_b)
    context_b = Context()
    loop_b = Loop(RuntimeError("AgentLoop must not run on handoff recovery"))
    lock_b = Lock()
    events_b = Events()
    worker_b = _worker(
        request,
        lifecycle=lifecycle,
        results=results,
        active=active,
        handoff=handoff_b,
        dead_letter=dead_letter,
        loop=loop_b,
        context=context_b,
        lock=lock_b,
        events=events_b,
    )

    outcome = await worker_b.handle(request)

    assert outcome.disposition is WorkerDisposition.DEAD_LETTERED
    assert outcome.attempt == 1
    assert outcome.lifecycle.state is TaskStatusState.FAILED
    assert loop_b.calls == 0
    assert context_b.calls == []
    assert len(dead_letter.calls) == 2
    replayed_request, replayed_attempt, replayed_error = dead_letter.calls[1]
    assert replayed_request == request
    assert replayed_attempt == 1
    assert getattr(replayed_error, "error_type") == "ValueError"
    assert getattr(replayed_error, "error_message") == "analysis invalid"
    assert await handoff_b.load_pending(key) is None
    assert active.active == set()
    assert active.release_calls == [key]
    assert len(lock_b.release_calls) == 1
    assert events_b.events[-1][1].stage is TaskStage.DEAD_LETTERED
    assert lifecycle.values[key].attempt == 1
    durable_b.close()


@pytest.mark.asyncio
async def test_handoff_first_closes_terminal_lifecycle_crash_window(tmp_path: Path) -> None:
    request = _request()
    key = request.task_key
    lifecycle = FailTerminalLifecycleOnce()
    results = SharedResults()
    active = SharedActive()
    active.active.add(key)
    dead_letter = DeadLetter(lifecycle)
    dead_letter.fail_first = False

    database_path = tmp_path / "handoff-first-crash-window.sqlite3"
    durable_a = SqliteCheckpointStore(database_path)
    repository_a = CheckpointRepository(
        durable=durable_a,
        cache=InMemoryHotCheckpointCache(),
    )
    handoff_a = CheckpointDeadLetterHandoffStore(repository_a)
    context_a = Context()
    loop_a = Loop(ValueError("analysis invalid"))
    worker_a = _worker(
        request,
        lifecycle=lifecycle,
        results=results,
        active=active,
        handoff=handoff_a,
        dead_letter=dead_letter,
        loop=loop_a,
        context=context_a,
        lock=Lock(),
        events=Events(),
    )

    with pytest.raises(RuntimeError, match="injected terminal lifecycle"):
        await worker_a.handle(request)

    interrupted = lifecycle.values[key]
    assert interrupted.state is TaskStatusState.PROCESSING
    assert interrupted.attempt == 1
    pending = await handoff_a.load_pending(key)
    assert pending is not None
    assert pending.attempt == 1
    assert dead_letter.calls == []
    assert active.active == {key}
    assert loop_a.calls == 1
    assert context_a.calls == [77]
    durable_a.close()

    durable_b = SqliteCheckpointStore(database_path)
    repository_b = CheckpointRepository(
        durable=durable_b,
        cache=InMemoryHotCheckpointCache(),
    )
    handoff_b = CheckpointDeadLetterHandoffStore(repository_b)
    context_b = Context()
    loop_b = Loop(RuntimeError("AgentLoop must not run during handoff recovery"))
    worker_b = _worker(
        request,
        lifecycle=lifecycle,
        results=results,
        active=active,
        handoff=handoff_b,
        dead_letter=dead_letter,
        loop=loop_b,
        context=context_b,
        lock=Lock(),
        events=Events(),
    )

    outcome = await worker_b.handle(request)

    assert outcome.disposition is WorkerDisposition.DEAD_LETTERED
    assert outcome.lifecycle.state is TaskStatusState.FAILED
    assert outcome.lifecycle.stage is TaskStage.DEAD_LETTERED
    assert len(dead_letter.calls) == 1
    persisted_before_publish = dead_letter.lifecycle_at_publish[0]
    assert persisted_before_publish.state is TaskStatusState.FAILED
    assert persisted_before_publish.stage is TaskStage.DEAD_LETTERED
    assert await handoff_b.load_pending(key) is None
    assert loop_b.calls == 0
    assert context_b.calls == []
    assert active.active == set()
    assert active.release_calls == [key]
    assert lifecycle.values[key].attempt == 1
    durable_b.close()

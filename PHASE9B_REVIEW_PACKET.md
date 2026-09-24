# Phase 9B Review Packet

## Target A

### File

src/dovideo/application/dispatch.py

### Symbols

TaskDispatchService, TaskDispatchService.dispatch, _publish_best_effort, _release, submit, AnalysisDispatchService, DispatchDisposition, ACTIVE_TTL_SECONDS.

### Source

~~~python
from __future__ import annotations
import asyncio
from dovideo.domain import TaskEvent
from .task_lifecycle import TaskLifecycle
from .value_objects import AnalysisRequest, DispatchDisposition
from .ports.tasks import (
    TaskActiveMarkerPort,
    TaskCompletionPort,
    TaskEventPublisherPort,
    TaskLifecyclePort,
    TaskQuotaPort,
)

ACTIVE_TTL_SECONDS = 6 * 60 * 60

class TaskDispatchService:
    """Reserve one task identity and publish its queue-neutral QUEUED event."""
    def __init__(
        self,
        active: TaskActiveMarkerPort,
        *,
        completion: TaskCompletionPort | None = None,
        quota: TaskQuotaPort | None = None,
        lifecycle: TaskLifecyclePort | None = None,
        events: TaskEventPublisherPort | None = None,
        active_ttl_seconds: float = ACTIVE_TTL_SECONDS,
    ) -> None:
        self._active = active
        self._completion = completion
        self._quota = quota
        self._lifecycle = lifecycle
        self._events = events
        self._active_ttl_seconds = active_ttl_seconds

    async def dispatch(self, request: AnalysisRequest) -> DispatchDisposition:
        """Return Java-shaped ACCEPTED/RATE_LIMITED/DUPLICATE/FAILED."""
        if not isinstance(request, AnalysisRequest):
            return DispatchDisposition.FAILED
        key = request.task_key
        reserved = False
        try:
            if self._completion is not None and await self._completion.is_completed(key):
                return DispatchDisposition.DUPLICATE
            reserved = await self._active.reserve(
                key,
                ttl_seconds=self._active_ttl_seconds,
            )
            if not reserved:
                return DispatchDisposition.DUPLICATE
            if self._quota is not None and not await self._quota.try_acquire(request):
                await self._release(key)
                reserved = False
                return DispatchDisposition.RATE_LIMITED
            queued = TaskLifecycle.new(key).queued()
            if self._lifecycle is not None:
                await self._lifecycle.save_lifecycle(queued)
            await self._publish_best_effort(key, TaskEvent.of(queued.status, queued.stage))
            return DispatchDisposition.ACCEPTED
        except asyncio.CancelledError:
            if reserved:
                await self._release(key)
            raise
        except Exception:
            if reserved:
                await self._release(key)
            return DispatchDisposition.FAILED

    async def _publish_best_effort(self, key, event: TaskEvent) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(key, event)
        except Exception:
            return

    async def _release(self, key) -> None:
        try:
            await self._active.release(key)
        except Exception:
            return

    submit = dispatch

AnalysisDispatchService = TaskDispatchService

__all__ = [
    "ACTIVE_TTL_SECONDS",
    "AnalysisDispatchService",
    "TaskDispatchService",
]
~~~

### Relevant Tests

- tests/application/test_task_worker_9b.py::test_dispatch_accepts_new_request_saves_queued_and_publishes_event
- tests/application/test_task_worker_9b.py::test_dispatch_active_completed_and_rejected_outcomes
- tests/application/test_task_worker_9b.py::test_dispatch_invalid_request_is_failed_without_transport_dependency

### Why Included

This is the transport-neutral submission boundary: completion/active duplicate checks, atomic reservation, quota rejection, queued lifecycle persistence, best-effort event publication, and rollback are all in one method.

## Target B

### File

src/dovideo/application/worker.py

### Symbols

WorkerDisposition, TaskWorker.handle, process/on_message aliases, _load_lifecycle, _recover_completed, _publish, _mark_completed, _refresh_active, _release_active, _result_text, _profile_for.

### Source

~~~python
COMPLETED_TTL_SECONDS = 7 * 24 * 60 * 60

class WorkerDisposition(str, Enum):
    COMPLETED = "COMPLETED"
    RETRY = "RETRY"
    DEAD_LETTERED = "DEAD_LETTERED"
    DUPLICATE = "DUPLICATE"
    LOCKED = "LOCKED"

class TaskWorker:
    async def handle(
        self,
        request: AnalysisRequest,
        *,
        profile: ModeProfile | None = None,
    ) -> WorkerOutcome:
        """Consume one request and return completed/retry/dead-letter outcome."""
        if not isinstance(request, AnalysisRequest):
            raise TypeError("request must be an AnalysisRequest")
        key = request.task_key
        token = await self._lock.acquire(key)
        if token is None:
            return WorkerOutcome(
                WorkerDisposition.LOCKED,
                TaskLifecycle.new(key, max_attempts=self._max_attempts),
            )
        current = TaskLifecycle.new(key, max_attempts=self._max_attempts)
        outcome: WorkerOutcome | None = None
        try:
            current = await self._load_lifecycle(key)
            marker_completed = (
                self._completion is not None
                and await self._completion.is_completed(key)
            )
            saved = await self._results.load_result(key)
            if saved is not None and saved.result is not None:
                outcome = await self._recover_completed(key, current, saved)
                return outcome
            if marker_completed and self._completion is not None:
                await self._completion.clear_completed(key)
            if current.terminal:
                disposition = (
                    WorkerDisposition.COMPLETED
                    if current.state is TaskStatusState.COMPLETED
                    else WorkerDisposition.DEAD_LETTERED
                )
                outcome = WorkerOutcome(disposition, current)
                return outcome
            started = current.begin_attempt()
            await self._lifecycle.save_lifecycle(started)
            await self._publish(
                key,
                TaskStatus.of(TaskStatus.State.PROCESSING, "视频分析任务开始执行"),
                TaskStage.CONSUMING,
            )
            context = await self._context.load_context(request.media.media_id)
            if context is None:
                raise ValueError("analysis context is unavailable")
            state = await self._agent_loop.run(
                context,
                media_id=request.media.media_id,
                profile=self._profile_for(request, profile),
            )
            if not isinstance(state, AgentState) or state.result is None:
                raise ValueError("analysis did not produce a result")
            await self._results.save_result(key, state)
            completed = started.complete(self._result_text(state))
            await self._lifecycle.save_lifecycle(completed)
            await self._mark_completed(key)
            await self._publish(key, TaskStatus.completed(state), TaskStage.COMPLETED)
            outcome = WorkerOutcome(WorkerDisposition.COMPLETED, completed, state)
            return outcome
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if "started" not in locals():
                raise
            if not self._is_permanent(error) and started.can_retry:
                retrying = started.retry()
                await self._lifecycle.save_lifecycle(retrying)
                await self._refresh_active(key)
                await self._publish(
                    key,
                    TaskStatus.of(TaskStatus.State.PROCESSING, "本次执行失败，等待消息队列重试"),
                    TaskStage.RETRYING,
                )
                outcome = WorkerOutcome(
                    WorkerDisposition.RETRY,
                    retrying,
                    error=error,
                )
                return outcome
            failed = started.fail(
                "分析失败，已进入人工处理队列",
                stage=TaskStage.DEAD_LETTERED,
            )
            await self._lifecycle.save_lifecycle(failed)
            if self._dead_letter is not None:
                await self._dead_letter.publish(
                    request,
                    attempt=failed.attempt,
                    error=error,
                )
            await self._publish(
                key,
                TaskStatus.of(TaskStatus.State.FAILED, "分析失败，已进入人工处理队列"),
                TaskStage.DEAD_LETTERED,
            )
            outcome = WorkerOutcome(
                WorkerDisposition.DEAD_LETTERED,
                failed,
                error=error,
            )
            return outcome
        finally:
            if outcome is None or outcome.disposition is not WorkerDisposition.RETRY:
                await self._release_active(key)
            try:
                await self._lock.release(key, token)
            except Exception:
                pass

    process = handle
    on_message = handle
    onMessage = handle

    async def _load_lifecycle(self, key) -> TaskLifecycle:
        loaded = await self._lifecycle.load_lifecycle(key)
        if loaded is not None:
            return loaded
        return TaskLifecycle.new(key, max_attempts=self._max_attempts)

    async def _recover_completed(
        self,
        key,
        current: TaskLifecycle,
        state: AgentState,
    ) -> WorkerOutcome:
        completed = current
        if current.state is not TaskStatusState.COMPLETED:
            completed = current.complete(self._result_text(state))
            await self._lifecycle.save_lifecycle(completed)
        await self._mark_completed(key)
        await self._publish(key, TaskStatus.completed(state), TaskStage.COMPLETED)
        return WorkerOutcome(
            WorkerDisposition.COMPLETED,
            completed,
            state,
            recovered=True,
        )

    async def _publish(self, key, status: TaskStatus, stage: TaskStage) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(key, TaskEvent.of(status, stage))
        except Exception:
            return

    async def _mark_completed(self, key) -> None:
        if self._completion is None:
            return
        try:
            await self._completion.mark_completed(
                key,
                ttl_seconds=self._completed_ttl_seconds,
            )
        except Exception:
            return

    async def _refresh_active(self, key) -> None:
        try:
            await self._active.refresh(key, ttl_seconds=self._active_ttl_seconds)
        except Exception:
            return

    async def _release_active(self, key) -> None:
        try:
            await self._active.release(key)
        except Exception:
            return

    @staticmethod
    def _result_text(state: AgentState) -> str:
        return state.result.to_markdown() if state.result is not None else ""

    @staticmethod
    def _profile_for(request: AnalysisRequest, profile: ModeProfile | None) -> ModeProfile | None:
        if profile is not None:
            return profile
        if request.mode is AnalysisMode.GENERAL:
            return None
        return ModeProfile(mode=request.mode)

AnalysisWorker = TaskWorker
AnalysisTaskWorker = TaskWorker
~~~

### Relevant Tests

- tests/application/test_task_worker_9b.py::test_worker_success_attempt_one_processing_completed_cleanup_and_entrypoint
- tests/application/test_task_worker_9b.py::test_worker_retry_attempt_one_and_two_are_nonterminal_redelivery_decisions
- tests/application/test_task_worker_9b.py::test_worker_attempt_three_dead_letters_and_becomes_terminal
- tests/application/test_task_worker_9b.py::test_worker_permanent_failure_dead_letters_on_first_attempt
- tests/application/test_task_worker_9b.py::test_worker_lock_contention_is_duplicate_safe_and_does_not_invoke_agent
- tests/application/test_task_worker_9b.py::test_worker_completed_checkpoint_recovers_without_context_or_model_call
- tests/application/test_task_worker_9b.py::test_worker_preserves_non_general_mode_when_profile_is_omitted

### Why Included

This is the complete worker control boundary around lock acquisition, checkpoint/result recovery, processing, existing AgentLoop entry, lifecycle/result persistence, retry/dead-letter decisions, events, and cleanup in finally.

## Target C

### File

src/dovideo/application/worker.py; DOVideo-AI/server/src/main/java/com/example/server/consumer/VideoAnalysisConsumer.java

### Symbols

TaskWorker._is_permanent; BaseException handling; asyncio.CancelledError, KeyboardInterrupt, SystemExit, ValueError, TypeError, PermissionError, LookupError, RuntimeError; Java VideoAnalysisConsumer.isPermanentFailure and MAX_CAUSE_DEPTH.

### Source

~~~python
    @staticmethod
    def _is_permanent(error: BaseException) -> bool:
        return isinstance(error, (ValueError, TypeError, PermissionError, LookupError))
~~~

~~~java
private static final int MAX_CAUSE_DEPTH = 16;

private boolean isPermanentFailure(Throwable error) {
    Throwable current = error;
    for (int depth = 0; current != null && depth < MAX_CAUSE_DEPTH; depth++) {
        if (current instanceof IllegalArgumentException
                || current instanceof SecurityException
                || current instanceof NoSuchElementException) {
            return true;
        }
        if (current.getCause() == current) break;
        current = current.getCause();
    }
    return false;
}
~~~

### Relevant Tests

- tests/application/test_task_worker_9b.py::test_worker_retry_attempt_one_and_two_are_nonterminal_redelivery_decisions
- tests/application/test_task_worker_9b.py::test_worker_attempt_three_dead_letters_and_becomes_terminal
- tests/application/test_task_worker_9b.py::test_worker_permanent_failure_dead_letters_on_first_attempt

### Why Included

The source shows the exception families that become terminal, the cancellation pass-through, and the Java bounded cause-chain classification used for the Python mapping.

## Target D

### File

src/dovideo/application/ports/tasks.py

### Symbols

TaskActivityPort, TaskDispatchPort, TaskEventPublisherPort, TaskLifecyclePort, TaskActiveMarkerPort, TaskCompletionPort, TaskLockPort, TaskQuotaPort, TaskDeadLetterPort, TaskResultPort, AgentLoopEntryPort.

### Source

~~~python
from __future__ import annotations
from typing import Protocol
from dovideo.domain import AgentState, ModeProfile, TaskEvent, VideoContext
from ..task_lifecycle import TaskLifecycle
from ..value_objects import AnalysisRequest, DispatchDisposition, TaskKey

class TaskActivityPort(Protocol):
    async def is_active(self, key: TaskKey) -> bool:
        ...

class TaskDispatchPort(Protocol):
    async def dispatch(self, request: AnalysisRequest) -> DispatchDisposition:
        ...

class TaskEventPublisherPort(Protocol):
    async def publish(self, key: TaskKey, event: TaskEvent) -> None:
        ...

class TaskLifecyclePort(Protocol):
    async def load_lifecycle(self, key: TaskKey) -> TaskLifecycle | None:
        ...
    async def save_lifecycle(self, lifecycle: TaskLifecycle) -> None:
        ...

class TaskActiveMarkerPort(Protocol):
    async def reserve(self, key: TaskKey, *, ttl_seconds: float) -> bool:
        ...
    async def is_active(self, key: TaskKey) -> bool:
        ...
    async def refresh(self, key: TaskKey, *, ttl_seconds: float) -> None:
        ...
    async def release(self, key: TaskKey) -> None:
        ...

class TaskCompletionPort(Protocol):
    async def is_completed(self, key: TaskKey) -> bool:
        ...
    async def mark_completed(self, key: TaskKey, *, ttl_seconds: float) -> None:
        ...
    async def clear_completed(self, key: TaskKey) -> None:
        ...

class TaskLockPort(Protocol):
    async def acquire(self, key: TaskKey) -> object | None:
        ...
    async def release(self, key: TaskKey, token: object) -> None:
        ...

class TaskQuotaPort(Protocol):
    async def try_acquire(self, request: AnalysisRequest) -> bool:
        ...

class TaskDeadLetterPort(Protocol):
    async def publish(
        self,
        request: AnalysisRequest,
        *,
        attempt: int,
        error: BaseException,
    ) -> None:
        ...

class TaskResultPort(Protocol):
    async def load_result(self, key: TaskKey) -> AgentState | None:
        ...
    async def save_result(self, key: TaskKey, state: AgentState) -> None:
        ...

class AgentLoopEntryPort(Protocol):
    async def run(
        self,
        context: VideoContext,
        media_id: int | None = None,
        profile: ModeProfile | None = None,
    ) -> AgentState:
        ...

~~~

### Relevant Tests

- tests/application/test_task_worker_9b.py::test_dispatch_accepts_new_request_saves_queued_and_publishes_event
- tests/application/test_task_worker_9b.py::test_worker_success_attempt_one_processing_completed_cleanup_and_entrypoint
- tests/application/test_task_worker_9b.py::test_worker_lock_contention_is_duplicate_safe_and_does_not_invoke_agent

### Why Included

These contracts keep queue, lock, active-marker, completion, dead-letter, result, event, and existing AgentLoop concerns injectable and transport-neutral.

## Target E

### File

tests/application/test_task_worker_9b.py

### Symbols

All helpers, fakes, and tests in the current Phase 9B dispatch/worker test module.

### Source

~~~python
"""Phase 9B dispatch and worker orchestration tests with transport-neutral fakes."""
from __future__ import annotations
from collections.abc import Awaitable, Callable
import pytest
from dovideo.application import (
    AnalysisRequest,
    DispatchDisposition,
    MediaRef,
    TaskDispatchService,
    TaskLifecycle,
    TaskWorker,
    WorkerDisposition,
)
from dovideo.domain import (
    AgentState,
    AnalysisMode,
    AnalysisResult,
    TaskStage,
    TaskStatusState,
    VideoContext,
    VideoSegment,
)
def _request(mode: AnalysisMode | None = AnalysisMode.GENERAL) -> AnalysisRequest:
    return AnalysisRequest(MediaRef(7, "memory://video", content_hash="a" * 32), "goal", mode)
def _context(request: AnalysisRequest) -> VideoContext:
    return VideoContext(
        source=request.media.source,
        user_goal=request.goal,
        segments=[VideoSegment(start_ms=0, end_ms=1000, transcript="evidence")],
    )
def _state(request: AnalysisRequest) -> AgentState:
    return AgentState(
        goal=request.goal,
        result=AnalysisResult(title="done", conclusions=["supported"]),
    )
class FakeActive:
    def __init__(self) -> None:
        self.active: set = set()
        self.reserve_calls: list[tuple[object, float]] = []
        self.refresh_calls: list[tuple[object, float]] = []
        self.release_calls: list[object] = []
    async def reserve(self, key, *, ttl_seconds: float) -> bool:
        self.reserve_calls.append((key, ttl_seconds))
        if key in self.active:
            return False
        self.active.add(key)
        return True
    async def is_active(self, key) -> bool:
        return key in self.active
    async def refresh(self, key, *, ttl_seconds: float) -> None:
        self.refresh_calls.append((key, ttl_seconds))
        self.active.add(key)
    async def release(self, key) -> None:
        self.release_calls.append(key)
        self.active.discard(key)
class FakeCompletion:
    def __init__(self) -> None:
        self.completed: set = set()
        self.mark_calls: list[tuple[object, float]] = []
        self.cleared: list[object] = []
    async def is_completed(self, key) -> bool:
        return key in self.completed
    async def mark_completed(self, key, *, ttl_seconds: float) -> None:
        self.mark_calls.append((key, ttl_seconds))
        self.completed.add(key)
    async def clear_completed(self, key) -> None:
        self.cleared.append(key)
        self.completed.discard(key)
class FakeLock:
    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.acquire_calls: list[object] = []
        self.release_calls: list[tuple[object, object]] = []
    async def acquire(self, key):
        self.acquire_calls.append(key)
        return None if self.busy else object()
    async def release(self, key, token) -> None:
        self.release_calls.append((key, token))
class FakeLifecycle:
    def __init__(self) -> None:
        self.values: dict[object, TaskLifecycle] = {}
        self.saves: list[TaskLifecycle] = []
    async def load_lifecycle(self, key):
        return self.values.get(key)
    async def save_lifecycle(self, lifecycle: TaskLifecycle) -> None:
        self.values[lifecycle.key] = lifecycle
        self.saves.append(lifecycle)
class FakeContext:
    def __init__(self) -> None:
        self.calls: list[int] = []
    async def load_context(self, media_id: int):
        self.calls.append(media_id)
        return self.context
    context: VideoContext
class FakeResults:
    def __init__(self) -> None:
        self.values: dict[object, AgentState] = {}
        self.saves: list[tuple[object, AgentState]] = []
    async def load_result(self, key):
        return self.values.get(key)
    async def save_result(self, key, state: AgentState) -> None:
        self.values[key] = state
        self.saves.append((key, state))
class FakeEvents:
    def __init__(self) -> None:
        self.events: list[tuple[object, object]] = []
    async def publish(self, key, event) -> None:
        self.events.append((key, event))
class FakeDeadLetter:
    def __init__(self) -> None:
        self.calls: list[tuple[AnalysisRequest, int, BaseException]] = []
    async def publish(self, request, *, attempt: int, error: BaseException) -> None:
        self.calls.append((request, attempt, error))
class FakeQuota:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0
    async def try_acquire(self, request) -> bool:
        self.calls += 1
        return self.allowed
class FakeAgentLoop:
    def __init__(self, outcomes: list[AgentState | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[VideoContext, int | None, object]] = []
    async def run(self, context, media_id=None, profile=None):
        self.calls.append((context, media_id, profile))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
def _worker(
    request: AnalysisRequest,
    outcomes: list[AgentState | BaseException],
    *,
    busy: bool = False,
):
    active = FakeActive()
    completion = FakeCompletion()
    lock = FakeLock(busy=busy)
    lifecycle = FakeLifecycle()
    context = FakeContext()
    context.context = _context(request)
    results = FakeResults()
    events = FakeEvents()
    dead = FakeDeadLetter()
    loop = FakeAgentLoop(outcomes)
    worker = TaskWorker(
        lock,
        active,
        lifecycle,
        context,
        loop,
        results,
        events=events,
        completion=completion,
        dead_letter=dead,
    )
    return worker, active, completion, lock, lifecycle, context, results, events, dead, loop
@pytest.mark.asyncio
async def test_dispatch_accepts_new_request_saves_queued_and_publishes_event() -> None:
    request = _request()
    active, lifecycle, events = FakeActive(), FakeLifecycle(), FakeEvents()
    service = TaskDispatchService(active, lifecycle=lifecycle, events=events)
    disposition = await service.dispatch(request)
    assert disposition is DispatchDisposition.ACCEPTED
    assert active.reserve_calls[0][0] == request.task_key
    assert lifecycle.values[request.task_key].stage is TaskStage.QUEUED
    assert events.events[0][1].state is TaskStatusState.QUEUED
@pytest.mark.asyncio
async def test_dispatch_active_completed_and_rejected_outcomes() -> None:
    request = _request()
    active, completion = FakeActive(), FakeCompletion()
    service = TaskDispatchService(active, completion=completion)
    assert await service.dispatch(request) is DispatchDisposition.ACCEPTED
    assert await service.dispatch(request) is DispatchDisposition.DUPLICATE
    other = _request(AnalysisMode.REVIEW)
    completion.completed.add(other.task_key)
    assert await service.dispatch(other) is DispatchDisposition.DUPLICATE
    rejected = TaskDispatchService(FakeActive(), quota=FakeQuota(False))
    assert await rejected.dispatch(_request()) is DispatchDisposition.RATE_LIMITED
@pytest.mark.asyncio
async def test_dispatch_invalid_request_is_failed_without_transport_dependency() -> None:
    service = TaskDispatchService(FakeActive())
    assert await service.dispatch(None) is DispatchDisposition.FAILED
@pytest.mark.asyncio
async def test_worker_success_attempt_one_processing_completed_cleanup_and_entrypoint() -> None:
    request = _request()
    state = _state(request)
    worker, active, completion, lock, lifecycle, context, results, events, dead, loop = _worker(
        request, [state]
    )
    outcome = await worker.handle(request)
    assert outcome.disposition is WorkerDisposition.COMPLETED
    assert outcome.attempt == 1
    assert outcome.terminal
    assert outcome.result is state
    assert loop.calls == [(context.context, 7, None)]
    assert context.calls == [7]
    assert results.saves == [(request.task_key, state)]
    assert [event.stage for _, event in events.events] == [TaskStage.CONSUMING, TaskStage.COMPLETED]
    assert lifecycle.values[request.task_key].state is TaskStatusState.COMPLETED
    assert completion.mark_calls and active.release_calls == [request.task_key]
    assert len(lock.release_calls) == 1
    assert not dead.calls
@pytest.mark.asyncio
async def test_worker_retry_attempt_one_and_two_are_nonterminal_redelivery_decisions() -> None:
    request = _request()
    worker, active, completion, lock, lifecycle, context, results, events, dead, loop = _worker(
        request,
        [RuntimeError("temporary-1"), RuntimeError("temporary-2"), RuntimeError("temporary-3")],
    )
    first = await worker.handle(request)
    second = await worker.handle(request)
    assert first.disposition is WorkerDisposition.RETRY
    assert first.attempt == 1 and not first.terminal and first.redeliver
    assert first.lifecycle.stage is TaskStage.RETRYING
    assert second.disposition is WorkerDisposition.RETRY
    assert second.attempt == 2 and not second.terminal
    assert len(loop.calls) == 2
    assert len(active.refresh_calls) == 2
    assert active.release_calls == []
    assert not completion.mark_calls
@pytest.mark.asyncio
async def test_worker_attempt_three_dead_letters_and_becomes_terminal() -> None:
    request = _request()
    worker, active, _completion, _lock, _lifecycle, _context, _results, events, dead, _loop = _worker(
        request,
        [RuntimeError("one"), RuntimeError("two"), RuntimeError("three")],
    )
    await worker.handle(request)
    await worker.handle(request)
    final = await worker.handle(request)
    assert final.disposition is WorkerDisposition.DEAD_LETTERED
    assert final.attempt == 3
    assert final.terminal
    assert final.lifecycle.stage is TaskStage.DEAD_LETTERED
    assert dead.calls[0][1] == 3
    assert events.events[-1][1].stage is TaskStage.DEAD_LETTERED
    assert active.release_calls == [request.task_key]
@pytest.mark.asyncio
async def test_worker_permanent_failure_dead_letters_on_first_attempt() -> None:
    request = _request()
    worker, active, _completion, _lock, _lifecycle, _context, _results, events, dead, loop = _worker(
        request, [ValueError("invalid input")]
    )
    outcome = await worker.handle(request)
    assert outcome.disposition is WorkerDisposition.DEAD_LETTERED
    assert outcome.attempt == 1
    assert len(loop.calls) == 1
    assert dead.calls[0][1] == 1
    assert events.events[-1][1].state is TaskStatusState.FAILED
    assert active.release_calls == [request.task_key]
@pytest.mark.asyncio
async def test_worker_lock_contention_is_duplicate_safe_and_does_not_invoke_agent() -> None:
    request = _request()
    worker, active, _completion, lock, _lifecycle, context, _results, events, _dead, loop = _worker(
        request, [_state(request)], busy=True
    )
    outcome = await worker.handle(request)
    assert outcome.disposition is WorkerDisposition.LOCKED
    assert not outcome.terminal
    assert loop.calls == []
    assert context.calls == []
    assert events.events == []
    assert active.release_calls == []
    assert lock.release_calls == []
@pytest.mark.asyncio
async def test_worker_completed_checkpoint_recovers_without_context_or_model_call() -> None:
    request = _request()
    state = _state(request)
    worker, active, completion, _lock, lifecycle, context, results, events, dead, loop = _worker(
        request, [RuntimeError("must not run")]
    )
    results.values[request.task_key] = state
    outcome = await worker.handle(request)
    assert outcome.disposition is WorkerDisposition.COMPLETED
    assert outcome.recovered
    assert outcome.result is state
    assert loop.calls == []
    assert context.calls == []
    assert lifecycle.values[request.task_key].state is TaskStatusState.COMPLETED
    assert events.events[0][1].stage is TaskStage.COMPLETED
    assert completion.mark_calls
    assert not dead.calls
@pytest.mark.asyncio
async def test_worker_preserves_non_general_mode_when_profile_is_omitted() -> None:
    request = _request(AnalysisMode.REVIEW)
    worker, _active, _completion, _lock, _lifecycle, _context, _results, _events, _dead, loop = _worker(
        request, [_state(request)]
    )
    await worker.handle(request)
    assert loop.calls[0][2] is not None
    assert loop.calls[0][2].mode is AnalysisMode.REVIEW
~~~

### Relevant Tests

- tests/application/test_task_worker_9b.py::test_dispatch_accepts_new_request_saves_queued_and_publishes_event
- tests/application/test_task_worker_9b.py::test_dispatch_active_completed_and_rejected_outcomes
- tests/application/test_task_worker_9b.py::test_dispatch_invalid_request_is_failed_without_transport_dependency
- tests/application/test_task_worker_9b.py::test_worker_success_attempt_one_processing_completed_cleanup_and_entrypoint
- tests/application/test_task_worker_9b.py::test_worker_retry_attempt_one_and_two_are_nonterminal_redelivery_decisions
- tests/application/test_task_worker_9b.py::test_worker_attempt_three_dead_letters_and_becomes_terminal
- tests/application/test_task_worker_9b.py::test_worker_permanent_failure_dead_letters_on_first_attempt
- tests/application/test_task_worker_9b.py::test_worker_lock_contention_is_duplicate_safe_and_does_not_invoke_agent
- tests/application/test_task_worker_9b.py::test_worker_completed_checkpoint_recovers_without_context_or_model_call
- tests/application/test_task_worker_9b.py::test_worker_preserves_non_general_mode_when_profile_is_omitted

### Why Included

This is the complete current 9B fake-based test module, including all transport fakes, lifecycle/result fixtures, dispatch cases, retry/dead-letter boundaries, lock contention, recovery, and mode propagation.

## Target F

### File

DOVideo-AI/server/src/main/java/com/example/server/service/AnalysisDispatchService.java; DOVideo-AI/server/src/main/java/com/example/server/consumer/VideoAnalysisConsumer.java; DOVideo-AI/server/src/main/java/com/example/server/utils/AnalysisTaskKeys.java

### Symbols

AnalysisDispatchService.submit, isActive, SubmissionResult; VideoAnalysisConsumer.onMessage, MAX_DELIVERY_ATTEMPTS, lock/active/completed/attempts handling, asyncAnalyze, retry/dead-letter/finally, isPermanentFailure; AnalysisTaskKeys.active, lock, completed, attempts.

### Source

~~~java
private static final Duration ACTIVE_TTL = Duration.ofHours(6);

public SubmissionResult submit(MediaFile mediaFile, String goal, AgentFeedback revision) {
    return submit(mediaFile, goal, revision, AnalysisMode.GENERAL);
}
public SubmissionResult submit(MediaFile mediaFile, String goal, AgentFeedback revision, AnalysisMode mode) {
    AnalysisMode resolvedMode = mode == null ? AnalysisMode.GENERAL : mode;
    Long mediaId = mediaFile.getId();
    String action = revision == null
            ? AnalysisTaskMsg.START_ANALYSIS
            : AnalysisTaskMsg.REVISE_ANALYSIS;
    String contentHash = revision == null ? contentHash(mediaId) : "media-" + mediaId;
    String goalDigest = AnalysisTaskKeys.goalDigest(goal, resolvedMode);
    String activeKey = AnalysisTaskKeys.active(contentHash, goalDigest);
    Boolean accepted = redisTemplate.opsForValue().setIfAbsent(
            activeKey, String.valueOf(mediaId), ACTIVE_TTL);
    if (!Boolean.TRUE.equals(accepted)) return SubmissionResult.DUPLICATE;
    try {
        if (!tryAcquireQuota(mediaFile.getUserId())) {
            redisTemplate.delete(activeKey);
            return SubmissionResult.RATE_LIMITED;
        }
        if (revision != null) aiService.stageRevision(revision, resolvedMode);
        rocketMQTemplate.convertAndSend(
                analysisTopic,
                new AnalysisTaskMsg(mediaId, action, contentHash, goal, resolvedMode.name()));
    } catch (RuntimeException e) {
        redisTemplate.delete(activeKey);
        if (revision != null) aiService.cancelStagedRevision(mediaId, goal, resolvedMode);
        return SubmissionResult.FAILED;
    }
    try {
        taskEventService.publishAnalysis(mediaId, goal, resolvedMode,
                TaskStatus.of(TaskStatus.State.QUEUED, "任务已进入异步分析队列"), TaskStage.QUEUED);
    } catch (RuntimeException eventError) {
        log.warn("analysis_queued_event_failed mediaId={} userId={}",
                mediaId, mediaFile.getUserId(), eventError);
    }
    return SubmissionResult.ACCEPTED;
}
public boolean isActive(Long mediaId, String goal) {
    return isActive(mediaId, goal, AnalysisMode.GENERAL);
}
public boolean isActive(Long mediaId, String goal, AnalysisMode mode) {
    String goalDigest = AnalysisTaskKeys.goalDigest(goal, mode);
    return Boolean.TRUE.equals(redisTemplate.hasKey(
            AnalysisTaskKeys.active(contentHash(mediaId), goalDigest)))
            || Boolean.TRUE.equals(redisTemplate.hasKey(
            AnalysisTaskKeys.active("media-" + mediaId, goalDigest)));
}
public enum SubmissionResult {
    ACCEPTED,
    RATE_LIMITED,
    DUPLICATE,
    FAILED
}
~~~

~~~java
private static final int MAX_DELIVERY_ATTEMPTS = 3;
private static final Duration ACTIVE_TTL = Duration.ofHours(6);
private static final int MAX_CAUSE_DEPTH = 16;

@Override
public void onMessage(AnalysisTaskMsg msg) {
    String rejection = rejectionReason(msg);
    if (rejection != null) {
        discardPoisonMessage(msg, rejection);
        return;
    }
    Long mediaId = msg.getMediaId();
    AnalysisMode mode = AnalysisMode.fromNullable(msg.getMode());
    String contentHash = AnalysisTaskKeys.normalizeContentHash(mediaId, msg.getContentHash());
    String goalDigest = AnalysisTaskKeys.goalDigest(msg.getUserGoal(), mode);
    String lockKey = AnalysisTaskKeys.lock(contentHash, goalDigest);
    String activeKey = AnalysisTaskKeys.active(contentHash, goalDigest);
    String completedKey = AnalysisTaskKeys.completed(contentHash, goalDigest);
    String attemptsKey = AnalysisTaskKeys.attempts(contentHash, goalDigest);
    RLock lock = redissonClient.getLock(lockKey);
    boolean acquired = false;
    boolean retrying = false;
    long attempt = 0;
    try {
        acquired = lock.tryLock();
        if (!acquired) return;
        if (!mediaService.exists(mediaId)) return;
        Long currentAttempt = redisTemplate.opsForValue().increment(attemptsKey);
        attempt = currentAttempt == null ? 1 : currentAttempt;
        redisTemplate.expire(attemptsKey, ACTIVE_TTL);
        taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
                TaskStatus.of(TaskStatus.State.PROCESSING, "视频分析任务开始执行"),
                TaskStage.CONSUMING);
        if (msg.isRevision()) {
            if (!checkpointService.beginStagedRevision(mediaId, msg.getUserGoal(), mode)) {
                throw new IllegalStateException("修订任务状态不存在，等待消息队列重试");
            }
            redisTemplate.delete(completedKey);
        } else {
            String completedMediaId = redisTemplate.opsForValue().get(completedKey);
            if (completedMediaId != null) {
                Long sourceMediaId = parseMediaId(completedMediaId, completedKey);
                AgentState reusable = sourceMediaId == null ? null
                        : checkpointService.loadResult(sourceMediaId, msg.getUserGoal(), mode);
                if (reusable != null && reusable.result() != null
                        && aiService.reuseResult(mediaId, sourceMediaId, reusable, mode)) {
                    taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
                            TaskStatus.completed(reusable), TaskStage.COMPLETED_REUSED);
                    return;
                }
                redisTemplate.delete(completedKey);
            }
        }
        saveStage(mediaId, msg.getUserGoal(), mode, TaskStage.CONSUMING);
        aiService.asyncAnalyze(mediaId, msg.getUserGoal(), mode);
        if (msg.isRevision()) {
            checkpointService.completeStagedRevision(mediaId, msg.getUserGoal(), mode);
        }
        if (!mediaService.exists(mediaId)) {
            mediaService.purgeRuntimeArtifacts(mediaId);
            return;
        }
        redisTemplate.opsForValue().set(
                completedKey, String.valueOf(mediaId), Duration.ofDays(7));
        AgentState completed = checkpointService.loadResult(mediaId, msg.getUserGoal(), mode);
        if (completed != null && completed.result() != null) {
            taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
                    TaskStatus.completed(completed), TaskStage.COMPLETED);
        }
    } catch (AgentLoopService.BudgetExceededException e) {
        saveStage(mediaId, msg.getUserGoal(), mode, TaskStage.BUDGET_EXHAUSTED);
        taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
                TaskStatus.of(TaskStatus.State.FAILED, e.getMessage()),
                TaskStage.BUDGET_EXHAUSTED);
        return;
    } catch (Exception e) {
        boolean permanent = isPermanentFailure(e);
        if (!permanent && acquired && attempt > 0 && attempt < MAX_DELIVERY_ATTEMPTS) {
            retrying = true;
            redisTemplate.expire(activeKey, ACTIVE_TTL);
            saveStage(mediaId, msg.getUserGoal(), mode, TaskStage.RETRYING);
            taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
                    TaskStatus.of(TaskStatus.State.PROCESSING, "本次执行失败，等待消息队列重试"),
                    TaskStage.RETRYING);
            throw new IllegalStateException("视频分析消费失败，交由 RocketMQ 重试", e);
        }
        if (acquired && (permanent || attempt >= MAX_DELIVERY_ATTEMPTS)) {
            try {
                try {
                    failedTaskService.record(msg, attempt, e);
                } catch (RuntimeException recordError) {
                    e.addSuppressed(recordError);
                }
                rocketMQTemplate.convertAndSend(deadLetterTopic, msg);
                saveStage(mediaId, msg.getUserGoal(), mode, TaskStage.DEAD_LETTERED);
                taskEventService.publishAnalysis(mediaId, msg.getUserGoal(), mode,
                        TaskStatus.of(TaskStatus.State.FAILED, "分析失败，已进入人工处理队列"),
                        TaskStage.DEAD_LETTERED);
                return;
            } catch (RuntimeException deadLetterError) {
                retrying = true;
                deadLetterError.addSuppressed(e);
                throw deadLetterError;
            }
        }
        throw new IllegalStateException("视频分析消费失败", e);
    } finally {
        if (acquired) {
            if (!retrying) redisTemplate.delete(java.util.List.of(activeKey, attemptsKey));
            if (lock.isHeldByCurrentThread()) {
                lock.unlock();
            }
        }
    }
}

private boolean isPermanentFailure(Throwable error) {
    Throwable current = error;
    for (int depth = 0; current != null && depth < MAX_CAUSE_DEPTH; depth++) {
        if (current instanceof IllegalArgumentException
                || current instanceof SecurityException
                || current instanceof NoSuchElementException) {
            return true;
        }
        if (current.getCause() == current) break;
        current = current.getCause();
    }
    return false;
}
~~~

~~~java
public static String active(String contentHash, String goalDigest) {
    return "analysis:active:" + contentHash + ":" + goalDigest;
}
public static String lock(String contentHash, String goalDigest) {
    return "lock:analysis:" + contentHash + ":" + goalDigest;
}
public static String completed(String contentScope, String goalDigest) {
    return "analysis:completed:" + contentScope + ":" + goalDigest;
}
public static String attempts(String contentScope, String goalDigest) {
    return "analysis:attempts:" + contentScope + ":" + goalDigest;
}
~~~

### Relevant Tests

- tests/application/test_task_worker_9b.py::test_dispatch_accepts_new_request_saves_queued_and_publishes_event
- tests/application/test_task_worker_9b.py::test_dispatch_active_completed_and_rejected_outcomes
- tests/application/test_task_worker_9b.py::test_worker_success_attempt_one_processing_completed_cleanup_and_entrypoint
- tests/application/test_task_worker_9b.py::test_worker_retry_attempt_one_and_two_are_nonterminal_redelivery_decisions
- tests/application/test_task_worker_9b.py::test_worker_attempt_three_dead_letters_and_becomes_terminal
- tests/application/test_task_worker_9b.py::test_worker_permanent_failure_dead_letters_on_first_attempt
- tests/application/test_task_worker_9b.py::test_worker_completed_checkpoint_recovers_without_context_or_model_call

### Why Included

These are the Java submission/consumer/key fragments that establish the identity, marker, lock, attempt, terminal, retry, and dead-letter boundaries represented by the Python seams.

## Crash / Recovery Matrix

| Crash point | Durable state | Active marker | Lock | Expected next delivery |
|---|---|---|---|---|
| Before dispatch reservation | No queued lifecycle from this call | Not reserved | Not acquired | A later submission can attempt the same key |
| After active reservation, before quota | No queued lifecycle | Reserved with dispatch TTL | Not acquired | Dispatch exception releases marker; a later submission can try |
| Worker lock contention | Existing lifecycle/result unchanged | Existing marker unchanged | Not acquired by this delivery | Broker/application can redeliver without model invocation |
| Lock acquired before lifecycle load | Previous durable checkpoint only | Held until worker finally releases it | Held, then released in finally | Redelivery reloads the previous durable state |
| Lifecycle attempt saved before PROCESSING event | PROCESSING/CONSUMING attempt is saved | Held | Held until finally | Redelivery sees the saved attempt and begins the next worker attempt |
| AgentLoop transient exception on attempt 1 or 2 | RETRYING lifecycle is saved | Refreshed and retained | Released | Redelivery begins the next attempt |
| Result saved before completed lifecycle | Result checkpoint exists; lifecycle may still be PROCESSING | Released by finally if no returned outcome | Released | Result load enters completed recovery without AgentLoop |
| Completed lifecycle before completed marker | Result and COMPLETED lifecycle exist | Released in finally | Released | Redelivery sees durable terminal state and does not invoke model |
| Attempt limit or permanent error before dead-letter publish | DEAD_LETTERED lifecycle is written after failure construction | Released after non-RETRY return, unless dead-letter publication raises | Released in finally | Terminal lifecycle is returned on subsequent delivery |
| Dead-letter publication failure | Failure lifecycle may be saved before publication; later event/cleanup not reached | Retry path retains/refreshes marker | Released in finally | Queue/application redelivery can re-enter the failure branch |
| Exception while final cleanup runs | Prior durable state remains whatever preceding save committed | Release is best effort in helper | Release catches ordinary release errors | Provider TTL/next delivery determines whether stale marker/lock is observed |

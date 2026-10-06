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
    mode_profile_for,
    BudgetExceededError,
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
    lease_seconds = None

    async def refresh(self, key, token):
        return True

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
        self.fail_completed_once = False

    async def load_lifecycle(self, key):
        return self.values.get(key)

    async def save_lifecycle(self, lifecycle: TaskLifecycle) -> None:
        if self.fail_completed_once and lifecycle.state is TaskStatusState.COMPLETED:
            self.fail_completed_once = False
            raise RuntimeError("completed checkpoint unavailable")
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
        self.failures: list[BaseException] = []

    async def publish(self, request, *, attempt: int, error: BaseException) -> None:
        self.calls.append((request, attempt, error))
        if self.failures:
            raise self.failures.pop(0)


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
    assert await service.dispatch(None) is DispatchDisposition.FAILED  # type: ignore[arg-type]


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
    assert loop.calls == [
        (context.context, 7, mode_profile_for(AnalysisMode.GENERAL))
    ]
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
async def test_budget_exhaustion_stops_without_repeating_expensive_model_calls() -> None:
    request = _request()
    worker, _active, _completion, _lock, _lifecycle, _context, _results, events, dead, loop = _worker(
        request, [BudgetExceededError("internal token arithmetic")]
    )

    outcome = await worker.handle(request)

    assert outcome.disposition is WorkerDisposition.DEAD_LETTERED
    assert outcome.attempt == 1
    assert outcome.lifecycle.stage is TaskStage.BUDGET_EXHAUSTED
    assert len(loop.calls) == len(dead.calls) == 1
    assert events.events[-1][1].stage is TaskStage.BUDGET_EXHAUSTED
    assert "缩小分析范围" in events.events[-1][1].message
    assert "arithmetic" not in events.events[-1][1].message


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
@pytest.mark.parametrize("mode", tuple(AnalysisMode))
async def test_worker_rebuilds_deterministic_mode_profile_when_omitted(mode) -> None:
    request = _request(mode)
    worker, _active, _completion, _lock, _lifecycle, _context, _results, _events, _dead, loop = _worker(
        request,
        [RuntimeError("temporary delivery failure"), _state(request)],
    )

    first = await worker.handle(request)
    second = await worker.handle(request)

    expected = mode_profile_for(mode)
    assert first.disposition is WorkerDisposition.RETRY
    assert second.disposition is WorkerDisposition.COMPLETED
    assert len(loop.calls) == 2
    assert loop.calls[0][2] == expected
    assert loop.calls[1][2] == expected


@pytest.mark.asyncio
async def test_worker_runtime_error_with_permanent_cause_dead_letters_first_attempt() -> None:
    request = _request()
    worker, _active, _completion, _lock, _lifecycle, _context, _results, _events, dead, loop = _worker(
        request, [RuntimeError("wrapped validation")]
    )
    error = loop.outcomes[0]
    assert isinstance(error, RuntimeError)
    error.__cause__ = ValueError("invalid request")

    outcome = await worker.handle(request)

    assert outcome.disposition is WorkerDisposition.DEAD_LETTERED
    assert outcome.attempt == 1
    assert len(loop.calls) == 1
    assert dead.calls[0][1] == 1


@pytest.mark.asyncio
async def test_worker_nested_transient_runtime_errors_remain_retryable() -> None:
    request = _request()
    inner = RuntimeError("provider transient")
    outer = RuntimeError("wrapped provider transient")
    outer.__cause__ = inner
    worker, _active, _completion, _lock, lifecycle, _context, _results, _events, _dead, loop = _worker(
        request, [outer]
    )

    outcome = await worker.handle(request)

    assert outcome.disposition is WorkerDisposition.RETRY
    assert outcome.attempt == 1
    assert not outcome.terminal
    assert lifecycle.values[request.task_key].stage is TaskStage.RETRYING
    assert len(loop.calls) == 1


def test_worker_permanent_cause_context_and_depth_are_bounded() -> None:
    contextual: RuntimeError
    try:
        try:
            raise ValueError("contextual validation")
        except ValueError:
            raise RuntimeError("context wrapper")
    except RuntimeError as error:
        contextual = error

    assert TaskWorker._is_permanent(contextual)

    cycle = RuntimeError("cycle")
    cycle.__cause__ = cycle
    assert not TaskWorker._is_permanent(cycle)

    deep: BaseException = ValueError("outside bounded depth")
    for index in range(16):
        wrapped = RuntimeError(f"layer-{index}")
        wrapped.__cause__ = deep
        deep = wrapped
    assert not TaskWorker._is_permanent(deep)


@pytest.mark.asyncio
async def test_worker_dead_letter_transport_failure_preserves_terminal_handoff() -> None:
    request = _request()
    worker, active, _completion, lock, lifecycle, _context, _results, events, dead, loop = _worker(
        request, [ValueError("invalid input")]
    )
    dead.failures.append(RuntimeError("dead-letter transport unavailable"))

    with pytest.raises(RuntimeError, match="dead-letter transport unavailable"):
        await worker.handle(request)

    assert len(loop.calls) == 1
    assert len(dead.calls) == 1
    assert lifecycle.values[request.task_key].state is TaskStatusState.FAILED
    assert lifecycle.values[request.task_key].stage is TaskStage.DEAD_LETTERED
    assert active.release_calls == []
    assert len(lock.release_calls) == 1

    outcome = await worker.handle(request)

    assert outcome.disposition is WorkerDisposition.DEAD_LETTERED
    assert len(dead.calls) == 2
    assert len(loop.calls) == 1
    assert lifecycle.values[request.task_key].state is TaskStatusState.FAILED
    assert active.release_calls == [request.task_key]
    assert len(lock.release_calls) == 2
    assert events.events[-1][1].stage is TaskStage.DEAD_LETTERED


@pytest.mark.asyncio
async def test_worker_result_checkpoint_recovers_after_completed_save_failure() -> None:
    request = _request()
    state = _state(request)
    worker, active, completion, lock, lifecycle, context, results, events, dead, loop = _worker(
        request, [state]
    )
    lifecycle.fail_completed_once = True

    first = await worker.handle(request)

    assert first.disposition is WorkerDisposition.RETRY
    assert first.attempt == 1
    assert len(loop.calls) == 1
    assert context.calls == [7]
    assert results.saves == [(request.task_key, state)]
    assert lifecycle.values[request.task_key].stage is TaskStage.RETRYING
    assert active.refresh_calls == [(request.task_key, 6 * 60 * 60)]
    assert active.release_calls == []
    assert len(lock.release_calls) == 1

    second = await worker.handle(request)

    assert second.disposition is WorkerDisposition.COMPLETED
    assert second.recovered
    assert len(loop.calls) == 1
    assert context.calls == [7]
    assert results.saves == [(request.task_key, state)]
    assert lifecycle.values[request.task_key].state is TaskStatusState.COMPLETED
    assert completion.mark_calls
    assert active.release_calls == [request.task_key]
    assert len(lock.release_calls) == 2
    assert events.events[-1][1].stage is TaskStage.COMPLETED
    assert not dead.calls

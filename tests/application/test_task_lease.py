"""Analysis lease fault windows, using an expiring provider-neutral lock."""
from __future__ import annotations

import asyncio
import time

import pytest

from dovideo.application import AgentCheckpointService, TaskLifecycle, TaskWorker, WorkerDisposition
from dovideo.application.task_lease import (
    TaskLeaseExpired, TaskLeaseKeeper, TaskLeaseLost, check_task_lease,
)
from dovideo.domain import TaskStatusState
from test_task_worker_9b import (
    FakeActive, FakeCompletion, FakeContext, FakeDeadLetter, FakeLifecycle,
    FakeResults, _context, _request, _state,
)


class ExpiringLock:
    lease_seconds = 0.3

    def __init__(self):
        self.token = None
        self.expires = 0.0
        self.refreshes = 0
        self.releases = 0
        self.faults = []
        self.stop_refresh = False
        self.on_release = None

    async def acquire(self, key):
        if self.token is not None and time.monotonic() < self.expires:
            return None
        self.token = object()
        self.expires = time.monotonic() + self.lease_seconds
        return self.token

    async def refresh(self, key, token):
        self.refreshes += 1
        if self.faults:
            fault = self.faults.pop(0)
            if isinstance(fault, Exception):
                raise fault
            if fault is False:
                self.token = object()  # Simulate a new owner; old release must leave it.
                return False
        if self.stop_refresh:
            raise ConnectionError("offline")
        if token is not self.token or time.monotonic() >= self.expires:
            return False
        self.expires = time.monotonic() + self.lease_seconds
        return True

    async def release(self, key, token):
        self.releases += 1
        if self.on_release:
            self.on_release()
        if token is self.token:
            self.token = None


class BlockingAgent:
    def __init__(self, error=None, *, swallow_cancel=False):
        self.entered = asyncio.Event()
        self.finish = asyncio.Event()
        self.calls = 0
        self.cancelled = False
        self.error = error
        self.swallow_cancel = swallow_cancel

    async def run(self, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            if not self.swallow_cancel:
                raise
        if self.error:
            raise self.error
        return _state(_request())


def setup_worker(lock=None, agent=None, **kwargs):
    lock = lock or ExpiringLock()
    agent = agent or BlockingAgent()
    active, lifecycle, completion, results, dead = (
        FakeActive(), FakeLifecycle(), FakeCompletion(), FakeResults(), FakeDeadLetter(),
    )
    active.active.add(_request().task_key)
    context = FakeContext()
    context.context = _context(_request())
    worker = TaskWorker(lock, active, lifecycle, context, agent, results,
                        completion=completion, dead_letter=dead, **kwargs)
    return worker, lock, agent, active, lifecycle, completion, results, dead


def no_keeper_tasks():
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("analysis-lease-")]


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_j_long_task_renews_and_excludes_duplicate_then_e_ordered_cleanup():
    worker, lock, agent, active, lifecycle, completion, results, _ = setup_worker()
    order = []
    save_result, save_lifecycle, mark_completed = results.save_result, lifecycle.save_lifecycle, completion.mark_completed
    async def result_saved(*args):
        await save_result(*args)
        order.append("result")
    async def lifecycle_saved(value):
        await save_lifecycle(value)
        if value.state is TaskStatusState.COMPLETED: order.append("lifecycle")
    async def marker_saved(*args, **kwargs):
        await mark_completed(*args, **kwargs)
        order.append("marker")
    results.save_result, lifecycle.save_lifecycle, completion.mark_completed = result_saved, lifecycle_saved, marker_saved
    task = asyncio.create_task(worker.handle(_request()))
    await agent.entered.wait()
    await until(lambda: lock.refreshes >= 4)  # Runs beyond the original 0.3s TTL.
    second_agent = BlockingAgent()
    second, *_ = setup_worker(lock=lock, agent=second_agent)
    assert (await second.handle(_request())).disposition is WorkerDisposition.LOCKED
    assert second_agent.calls == 0
    assert agent.calls == 1

    def released():
        assert order == ["result", "lifecycle", "marker"]
        assert results.saves
        assert lifecycle.saves[-1].state is TaskStatusState.COMPLETED
        assert completion.mark_calls
        no_keeper_tasks()
    lock.on_release = released
    agent.finish.set()
    assert (await task).disposition is WorkerDisposition.COMPLETED
    assert not active.active
    calls = lock.refreshes
    await asyncio.sleep(lock.lease_seconds / 2)
    assert calls == lock.refreshes
    assert lock.token is None
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_b_stopped_renewal_leaves_finite_ttl_for_next_owner():
    lock = ExpiringLock()
    # Crash equivalent: stop the keeper without executing release.
    token = await lock.acquire(_request().task_key)
    keeper = TaskLeaseKeeper(lock, _request().task_key, token, acquired_at=time.monotonic())
    agent = BlockingAgent()
    running = asyncio.create_task(keeper.run(lambda: agent.run()))
    await until(lambda: lock.refreshes >= 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    calls = lock.refreshes
    await asyncio.sleep(lock.lease_seconds + 0.05)
    assert lock.refreshes == calls and lock.releases == 0
    assert await lock.acquire(_request().task_key) is not None
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_f_retry_stops_renewal_preserves_active_and_next_delivery_succeeds():
    worker, lock, agent, active, lifecycle, _, _, _ = setup_worker(agent=BlockingAgent(RuntimeError("temporary")))
    running = asyncio.create_task(worker.handle(_request()))
    await until(lambda: lock.refreshes >= 1)
    agent.finish.set()
    outcome = await running
    assert outcome.disposition is WorkerDisposition.RETRY and outcome.attempt == 1
    assert active.refresh_calls and active.active
    assert lock.releases == 1
    no_keeper_tasks()
    agent.error = None
    assert (await worker.handle(_request())).attempt == 2
    assert lifecycle.saves[-1].state is TaskStatusState.COMPLETED
    no_keeper_tasks()


class Handoff:
    def __init__(self): self.pending = None
    async def load_pending(self, key): return self.pending
    async def save_pending(self, pending): self.pending = pending
    async def clear_pending(self, key): self.pending = None


@pytest.mark.asyncio
async def test_g_max_attempt_failure_keeps_durable_handoff_then_recovers_without_agent():
    handoff = Handoff()
    worker, lock, agent, active, lifecycle, _, _, dead = setup_worker(
        agent=BlockingAgent(RuntimeError("exhausted")), max_attempts=1, dead_letter_handoff=handoff,
    )
    dead.failures = [OSError("broker down")]
    running = asyncio.create_task(worker.handle(_request()))
    await until(lambda: lock.refreshes >= 1)
    agent.finish.set()
    with pytest.raises(OSError): await running
    assert handoff.pending is not None and active.active
    assert lifecycle.saves[-1].state is TaskStatusState.FAILED
    assert lock.releases == 1
    no_keeper_tasks()
    assert (await worker.handle(_request())).disposition is WorkerDisposition.DEAD_LETTERED
    assert agent.calls == 1 and handoff.pending is None and not active.active
    no_keeper_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("swallow_cancel", [False, True])
async def test_h_lost_owner_cannot_commit_even_if_agent_swallows_cancellation(swallow_cancel):
    lock = ExpiringLock()
    lock.faults = [False]
    worker, _, agent, active, lifecycle, completion, results, dead = setup_worker(
        lock, BlockingAgent(swallow_cancel=swallow_cancel),
    )
    with pytest.raises(TaskLeaseLost): await worker.handle(_request())
    assert agent.cancelled and active.active
    assert not results.saves and not completion.mark_calls and not dead.calls
    assert lifecycle.saves[-1].state is TaskStatusState.PROCESSING
    assert lock.token is not None and lock.releases == 1
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_i_transient_refresh_exception_recovers_within_known_lease(caplog):
    lock = ExpiringLock()
    lock.faults = [ConnectionError("brief outage")]
    worker, _, agent, *_ = setup_worker(lock)
    running = asyncio.create_task(worker.handle(_request()))
    await until(lambda: lock.refreshes >= 3)
    agent.finish.set()
    assert (await running).disposition is WorkerDisposition.COMPLETED
    assert "refresh failed" in caplog.text
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_i_persistent_outage_stops_before_final_commit_and_keeps_recovery_marker():
    lock = ExpiringLock()
    lock.stop_refresh = True
    worker, _, agent, active, lifecycle, completion, results, _ = setup_worker(lock)
    with pytest.raises(TaskLeaseExpired): await worker.handle(_request())
    assert agent.cancelled and active.active
    assert not results.saves and not completion.mark_calls
    assert lifecycle.saves[-1].state is TaskStatusState.PROCESSING
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_i_hung_refresh_is_bounded_by_known_expiry():
    lock = ExpiringLock()
    async def hung(*args): await asyncio.Event().wait()
    lock.refresh = hung
    worker, *_ = setup_worker(lock)
    async with asyncio.timeout(2):
        with pytest.raises(TaskLeaseExpired): await worker.handle(_request())
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_cancel_stops_keeper_releases_lock_and_preserves_recovery_marker():
    worker, lock, agent, active, *_ = setup_worker()
    running = asyncio.create_task(worker.handle(_request()))
    await agent.entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError): await running
    assert active.active and lock.releases == 1 and lock.token is None
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_stale_and_unexpected_early_exception_also_join_keeper():
    worker, lock, _, _, lifecycle, *_ = setup_worker()
    lifecycle.values[_request().task_key] = TaskLifecycle.new(_request().task_key, request_id="other")
    assert (await worker.handle(_request())).disposition is WorkerDisposition.STALE
    no_keeper_tasks()
    async def broken(key): raise OSError("read failed")
    lifecycle.load_lifecycle = broken
    with pytest.raises(OSError): await worker.handle(_request())
    assert lock.releases == 2
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_checkpoint_and_execution_record_guards_block_agent_owned_writes_after_loss():
    from dovideo.application.execution_record import ExecutionRecordService
    calls = []
    class Repository:
        def write(self, *args): calls.append("checkpoint")
        def create(self, *args): calls.append("history")
    checkpoint = AgentCheckpointService(Repository())
    records = ExecutionRecordService(Repository())
    lock = ExpiringLock()
    lock.faults = [False]
    async def stubborn():
        try: await asyncio.Event().wait()
        except asyncio.CancelledError: pass
        with pytest.raises(TaskLeaseLost): await checkpoint.save_result(_request().task_key, _state(_request()))
        with pytest.raises(TaskLeaseLost): await records.complete("execution", _state(_request()))
        check_task_lease()
    token = await lock.acquire(_request().task_key)
    keeper = TaskLeaseKeeper(lock, _request().task_key, token, acquired_at=time.monotonic())
    with pytest.raises(TaskLeaseLost): await keeper.run(stubborn)
    assert calls == []
    check_task_lease()  # Delivery ContextVar was reset.
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_event_loop_stall_is_detected_at_commit_boundary_without_renewal_turn():
    class StallingAgent:
        async def run(self, *args, **kwargs):
            time.sleep(0.35)  # Fault injection: keeper cannot be scheduled.
            return _state(_request())
    worker, _, _, active, _, completion, results, _ = setup_worker(agent=StallingAgent())
    with pytest.raises(TaskLeaseExpired): await worker.handle(_request())
    assert not results.saves and not completion.mark_calls and active.active
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_acquisition_latency_counts_against_validity_without_starting_agent():
    lock = ExpiringLock()
    acquire = lock.acquire
    async def delayed(key):
        token = await acquire(key)
        await asyncio.sleep(0.35)
        return token
    lock.acquire = delayed
    worker, _, agent, active, lifecycle, *_ = setup_worker(lock)
    with pytest.raises(TaskLeaseExpired): await worker.handle(_request())
    assert agent.calls == 0 and not lifecycle.saves
    assert active.active and lock.releases == 1
    no_keeper_tasks()


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan"), True])
@pytest.mark.asyncio
async def test_invalid_provider_lease_never_starts_work_and_releases_token(duration):
    lock = ExpiringLock()
    # Keep acquire usable; inject invalid contract after acquisition.
    acquire = lock.acquire
    async def acquired(key):
        token = await acquire(key)
        lock.lease_seconds = duration
        return token
    lock.acquire = acquired
    worker, _, agent, *_ = setup_worker(lock)
    with pytest.raises(ValueError): await worker.handle(_request())
    assert lock.releases == 1 and agent.calls == 0
    no_keeper_tasks()


@pytest.mark.asyncio
async def test_actual_r4_composition_injects_finite_lease_and_renews_without_worker_opt_in(monkeypatch):
    from types import SimpleNamespace
    import dovideo.infrastructure.r4_runtime as runtime
    from dovideo.infrastructure.celery_transport import CeleryTransportSettings
    lock, agent = ExpiringLock(), BlockingAgent()
    active, lifecycle, completion = FakeActive(), FakeLifecycle(), FakeCompletion()
    class Checkpoint(FakeResults, FakeContext):
        def __init__(self):
            FakeResults.__init__(self)
            FakeContext.__init__(self)
    checkpoint = Checkpoint()
    checkpoint.context = _context(_request())
    infrastructure = SimpleNamespace(
        task_lock=lock, active_marker=active, completion_marker=completion,
        checkpoint_repository=object(), execution_record_repository=object(),
        redis_client=object(), failed_task_store=object(), vector_index=object(),
    )
    monkeypatch.setattr(runtime, "AgentCheckpointService", lambda *args: checkpoint)
    monkeypatch.setattr(runtime.CheckpointTaskLifecycleStore, "from_environment", lambda *args, **kwargs: lifecycle)
    monkeypatch.setattr(runtime, "ExecutionRecordService", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "R3RedisTaskEventPublisher", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "CheckpointDeadLetterHandoffStore", lambda *args: Handoff())
    monkeypatch.setattr(runtime, "create_r4_provider_stack", lambda *args, **kwargs: SimpleNamespace(agent_loop=agent))
    monkeypatch.setattr(runtime, "R4MediaPipeline", lambda *args: object())
    monkeypatch.setattr(runtime.AnalysisSettings, "from_environment", lambda **kwargs: object())
    composed = runtime.R4WorkerRuntime.from_environment(
        settings=CeleryTransportSettings("amqp://test:test@localhost//"), infrastructure=infrastructure,
    )
    assert composed.worker._lock is infrastructure.task_lock
    running = asyncio.create_task(composed.worker.handle(_request()))
    await until(lambda: lock.refreshes >= 4)
    assert (await composed.worker.handle(_request())).disposition is WorkerDisposition.LOCKED
    agent.finish.set()
    assert (await running).disposition is WorkerDisposition.COMPLETED
    no_keeper_tasks()

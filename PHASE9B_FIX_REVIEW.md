# Phase 9B-FIX Review Evidence
## Target A
### File
src/dovideo/application/worker.py
tests/application/test_task_worker_9b.py

### Symbols
MAX_CAUSE_DEPTH, TaskWorker._is_permanent, direct permanent failure,
wrapped permanent failure, nested transient failure, context traversal,
self-cycle protection, and bounded cause depth.

### Source
~~~python
MAX_CAUSE_DEPTH = 16

    @staticmethod
    def _is_permanent(error: BaseException) -> bool:
        # Java bounds cause-chain inspection at MAX_CAUSE_DEPTH.  Prefer an
        # explicit Python cause, then follow an implicit context only when no
        # explicit cause is present.  Identity tracking handles self-cycles
        # and malformed multi-node cycles without recursion.
        current: BaseException | None = error
        seen: set[int] = set()
        for _ in range(MAX_CAUSE_DEPTH):
            if current is None:
                return False
            identity = id(current)
            if identity in seen:
                return False
            seen.add(identity)
            if isinstance(current, (ValueError, TypeError, PermissionError, LookupError)):
                return True
            cause = current.__cause__
            if cause is not None and cause is not current and id(cause) not in seen:
                current = cause
                continue
            context = current.__context__
            if context is None or context is current or id(context) in seen:
                return False
            current = context
        return False
~~~

~~~python
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
~~~

### Relevant Tests
- tests/application/test_task_worker_9b.py::test_worker_permanent_failure_dead_letters_on_first_attempt
- tests/application/test_task_worker_9b.py::test_worker_runtime_error_with_permanent_cause_dead_letters_first_attempt
- tests/application/test_task_worker_9b.py::test_worker_nested_transient_runtime_errors_remain_retryable
- tests/application/test_task_worker_9b.py::test_worker_permanent_cause_context_and_depth_are_bounded

### Why Included
These excerpts show the outer exception check, the existing permanent family,
cause/context preference, 16-level bound, and identity-based cycle stop.

## Target B
### File
src/dovideo/application/worker.py

### Symbols
TaskWorker.__init__ pending state, handle pending recognition,
terminal failure branch, nested dead-letter publish exception,
pending_dead_letter cleanup flag, finally lock/active cleanup, and
_retry_pending_dead_letter.

### Source
~~~python
        self._pending_dead_letters: dict[
            object,
            tuple[AnalysisRequest, int, BaseException],
        ] = {}
        current = TaskLifecycle.new(key, max_attempts=self._max_attempts)
        outcome: WorkerOutcome | None = None
        pending_dead_letter = False
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
            if key in self._pending_dead_letters:
                pending_dead_letter = True
                outcome = await self._retry_pending_dead_letter(key, current)
                pending_dead_letter = False
                return outcome
            if marker_completed and self._completion is not None:
                await self._completion.clear_completed(key)
~~~

~~~python
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
                try:
                    await self._dead_letter.publish(
                        request,
                        attempt=failed.attempt,
                        error=error,
                    )
                except BaseException:
                    # The analysis is already terminal.  Keep the active
                    # marker and the original failure so a later delivery can
                    # finish the transport handoff without rerunning analysis.
                    self._pending_dead_letters[key] = (
                        request,
                        failed.attempt,
                        error,
                    )
                    pending_dead_letter = True
                    raise
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
            if (
                (outcome is None or outcome.disposition is not WorkerDisposition.RETRY)
                and not pending_dead_letter
            ):
                await self._release_active(key)
            try:
                await self._lock.release(key, token)
            except Exception:
                pass
~~~

~~~python
    async def _retry_pending_dead_letter(
        self,
        key,
        current: TaskLifecycle,
    ) -> WorkerOutcome:
        request, attempt, error = self._pending_dead_letters[key]
        if self._dead_letter is not None:
            await self._dead_letter.publish(
                request,
                attempt=attempt,
                error=error,
            )
        # Remove the pending handoff before best-effort event publication:
        # the durable lifecycle is already terminal and a cancellation while
        # notifying must not cause a duplicate dead-letter submission.
        self._pending_dead_letters.pop(key, None)
        await self._publish(
            key,
            TaskStatus.of(TaskStatus.State.FAILED, "分析失败，已进入人工处理队列"),
            TaskStage.DEAD_LETTERED,
        )
        return WorkerOutcome(
            WorkerDisposition.DEAD_LETTERED,
            current,
            error=error,
        )
~~~

### Relevant Tests
- tests/application/test_task_worker_9b.py::test_worker_permanent_failure_dead_letters_on_first_attempt
- tests/application/test_task_worker_9b.py::test_worker_dead_letter_transport_failure_preserves_terminal_handoff

### Why Included
This is the complete terminal branch and its pending handoff helper, including
the exception boundary, active retention, lock release, and later publish.

## Target C
### File
tests/application/test_task_worker_9b.py

### Symbols
FakeDeadLetter failure injection and
test_worker_dead_letter_transport_failure_preserves_terminal_handoff.

### Source
~~~python
class FakeDeadLetter:
    def __init__(self) -> None:
        self.calls: list[tuple[AnalysisRequest, int, BaseException]] = []
        self.failures: list[BaseException] = []
    async def publish(self, request, *, attempt: int, error: BaseException) -> None:
        self.calls.append((request, attempt, error))
        if self.failures:
            raise self.failures.pop(0)
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
~~~

### Relevant Tests
- tests/application/test_task_worker_9b.py::test_worker_dead_letter_transport_failure_preserves_terminal_handoff

### Why Included
The complete regression test records first-publish failure, original terminal
state, no active release, lock release, one AgentLoop call, and later handoff.

## Target D
### File
tests/application/test_task_worker_9b.py

### Symbols
FakeLifecycle.fail_completed_once, FakeResults.save_result, and
test_worker_result_checkpoint_recovers_after_completed_save_failure.

### Source
~~~python
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
class FakeResults:
    def __init__(self) -> None:
        self.values: dict[object, AgentState] = {}
        self.saves: list[tuple[object, AgentState]] = []
    async def load_result(self, key):
        return self.values.get(key)
    async def save_result(self, key, state: AgentState) -> None:
        self.values[key] = state
        self.saves.append((key, state))
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
~~~

### Relevant Tests
- tests/application/test_task_worker_9b.py::test_worker_result_checkpoint_recovers_after_completed_save_failure

### Why Included
The fake fails only the completed lifecycle write after result save; the full
test then demonstrates result-first recovery and the absence of a second
context or AgentLoop call.

## Pending Handoff Durability Note
- Pending state currently lives only in TaskWorker._pending_dead_letters,
  an in-memory dictionary keyed by TaskKey.
- A process restart loses that pending tuple, including the original error.
- The durable lifecycle remains FAILED with DEAD_LETTERED stage.
- On a restarted worker, the pending branch is not entered.
- The next delivery reaches the existing terminal-lifecycle branch and returns
  DEAD_LETTERED without calling TaskDeadLetterPort.publish().
- Therefore the durable lifecycle alone can appear terminal while the
  dead-letter transport handoff was not completed.
- In the Java baseline, a dead-letter publish RuntimeException sets retrying,
  rethrows from VideoAnalysisConsumer, and finally retains active/attempt
  state; RocketMQ redelivery enters the consumer again.

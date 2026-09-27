"""Provider-neutral Phase 9B analysis worker orchestration.

``TaskWorker`` models the Java consumer's control flow around existing
checkpoint and AgentLoop ports.  It returns a delivery decision instead of
raising a broker-specific retry exception; a future adapter can translate
``RETRY`` into its queue's redelivery acknowledgement semantics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from dovideo.domain import (
    AgentState,
    ModeProfile,
    TaskEvent,
    TaskStage,
    TaskStatus,
    TaskStatusState,
)

from .ports.checkpoint import ContextCheckpointPort
from .ports.tasks import (
    AgentLoopEntryPort,
    TaskActiveMarkerPort,
    TaskCompletionPort,
    TaskDeadLetterPort,
    TaskDeadLetterHandoffPort,
    TaskEventPublisherPort,
    TaskLifecyclePort,
    TaskLockPort,
    TaskResultPort,
)
from .dead_letter_handoff import PendingDeadLetterHandoff
from .errors import BudgetExceededError
from .mode_profiles import mode_profile_for
from .task_lifecycle import DEFAULT_MAX_ATTEMPTS, TaskLifecycle
from .value_objects import AnalysisRequest


COMPLETED_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_CAUSE_DEPTH = 16


class WorkerDisposition(str, Enum):
    """Queue-neutral result of one delivery attempt."""

    COMPLETED = "COMPLETED"
    RETRY = "RETRY"
    DEAD_LETTERED = "DEAD_LETTERED"
    DUPLICATE = "DUPLICATE"
    LOCKED = "LOCKED"


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """Immutable worker decision handed to a future transport adapter."""

    disposition: WorkerDisposition
    lifecycle: TaskLifecycle
    result: AgentState | None = None
    error: BaseException | None = None
    recovered: bool = False

    @property
    def attempt(self) -> int:
        return self.lifecycle.attempt

    @property
    def terminal(self) -> bool:
        return self.lifecycle.terminal

    @property
    def retryable(self) -> bool:
        return self.disposition is WorkerDisposition.RETRY

    @property
    def redeliver(self) -> bool:
        return self.retryable


class TaskWorker:
    """Run one accepted request with Java-compatible lock/retry boundaries."""

    def __init__(
        self,
        lock: TaskLockPort,
        active: TaskActiveMarkerPort,
        lifecycle: TaskLifecyclePort,
        context: ContextCheckpointPort,
        agent_loop: AgentLoopEntryPort,
        results: TaskResultPort,
        *,
        events: TaskEventPublisherPort | None = None,
        completion: TaskCompletionPort | None = None,
        dead_letter: TaskDeadLetterPort | None = None,
        dead_letter_handoff: TaskDeadLetterHandoffPort | None = None,
        execution_records: Any | None = None,
        execution_record_service: Any | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        active_ttl_seconds: float = 6 * 60 * 60,
        completed_ttl_seconds: float = COMPLETED_TTL_SECONDS,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        self._lock = lock
        self._active = active
        self._lifecycle = lifecycle
        self._context = context
        self._agent_loop = agent_loop
        self._results = results
        self._events = events
        self._completion = completion
        self._dead_letter = dead_letter
        self._dead_letter_handoff = dead_letter_handoff
        self._execution_records = (
            execution_record_service
            if execution_record_service is not None
            else execution_records
        )
        self._max_attempts = max_attempts
        self._active_ttl_seconds = active_ttl_seconds
        self._completed_ttl_seconds = completed_ttl_seconds
        self._pending_dead_letters: dict[
            object,
            tuple[AnalysisRequest, int, BaseException],
        ] = {}

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
        pending_dead_letter = False
        try:
            current = await self._load_lifecycle(key)
            pending = await self._load_pending_dead_letter(key)
            if pending is not None:
                # A terminal analysis failure whose transport handoff is
                # pending must be resumed before result/terminal shortcuts.
                # No new attempt, context load, or AgentLoop invocation is
                # allowed on this path.
                pending_dead_letter = True
                outcome = await self._retry_pending_dead_letter(key, current, pending)
                pending_dead_letter = False
                return outcome
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
            if self._execution_records is not None:
                source_revision = context.source_revision.strip()
                if not source_revision:
                    source_revision = next(
                        (
                            segment.source_revision.strip()
                            for segment in context.segments
                            if segment.source_revision.strip()
                        ),
                        "",
                    )
                load_execution = getattr(self._execution_records, "load_for_task", None)
                existing_execution = (
                    await load_execution(key)
                    if callable(load_execution)
                    else None
                )
                if existing_execution is None and await self._legacy_checkpoint_exists(key):
                    # A pre-X2-B plan/draft checkpoint is latest recovery
                    # state only.  It cannot be silently promoted into a
                    # complete historical execution record.
                    raise RuntimeError(
                        "legacy checkpoint has no durable X2-B execution history"
                    )
                execution = await self._execution_records.start_or_resume(
                    key,
                    # AgentLoop binds the historical header to
                    # ``VideoContext.source``.  The context checkpoint is
                    # built from this same authorized media source; the
                    # content hash remains part of the X2-A-derived
                    # ``source_revision`` rather than creating a second
                    # media-identity spelling across the worker boundary.
                    media_identity=request.media.source,
                    source_revision=source_revision,
                    worker_attempt=started.attempt,
                    request_id=request.request_id,
                )
                execution_status = getattr(execution, "status", None)
                if getattr(execution_status, "value", execution_status) != "STARTED":
                    raise RuntimeError("current execution record is already terminal")
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

            budget_exhausted = isinstance(error, BudgetExceededError)
            public_failure = (
                "本次分析超过执行预算，请缩小分析范围后重试"
                if budget_exhausted
                else "分析失败，已进入人工处理队列"
            )
            failure_stage = (
                TaskStage.BUDGET_EXHAUSTED
                if budget_exhausted
                else TaskStage.DEAD_LETTERED
            )
            failed = started.fail(public_failure, stage=failure_stage)
            if self._execution_records is not None:
                await self._execution_records.fail_for_task(
                    key,
                    error,
                    classification=type(error).__name__,
                )
            if self._dead_letter_handoff is not None:
                # Write the recoverable request first. If the process stops
                # before the terminal lifecycle write, the next delivery sees
                # this handoff and completes both the lifecycle and DLQ path.
                # Keep the active marker while persistence/publication is
                # incomplete.
                pending_dead_letter = True
                handoff = PendingDeadLetterHandoff.from_request(
                    request,
                    attempt=failed.attempt,
                    error=error,
                )
                await self._dead_letter_handoff.save_pending(handoff)
                await self._lifecycle.save_lifecycle(failed)
                if self._dead_letter is None:
                    raise RuntimeError("dead-letter publisher is required")
                try:
                    await self._dead_letter.publish(
                        request,
                        attempt=failed.attempt,
                        error=error,
                    )
                except BaseException:
                    # The durable handoff remains for a later delivery.  Do
                    # not turn a transport error into another analysis
                    # attempt or a RETRYING lifecycle.
                    raise
                await self._dead_letter_handoff.clear_pending(key)
                pending_dead_letter = False
            else:
                await self._lifecycle.save_lifecycle(failed)
            if self._dead_letter_handoff is None and self._dead_letter is not None:
                try:
                    await self._dead_letter.publish(
                        request,
                        attempt=failed.attempt,
                        error=error,
                    )
                except BaseException:
                    # Legacy in-process fallback retained for callers that do
                    # not yet inject the durable handoff adapter.  Durable
                    # callers never use this dictionary for correctness.
                    self._pending_dead_letters[key] = (
                        request,
                        failed.attempt,
                        error,
                    )
                    pending_dead_letter = True
                    raise
            await self._publish(
                key,
                TaskStatus.of(TaskStatus.State.FAILED, public_failure),
                failure_stage,
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

    process = handle
    on_message = handle
    onMessage = handle

    async def _load_lifecycle(self, key) -> TaskLifecycle:
        loaded = await self._lifecycle.load_lifecycle(key)
        if loaded is not None:
            return loaded
        return TaskLifecycle.new(key, max_attempts=self._max_attempts)

    async def _legacy_checkpoint_exists(self, key) -> bool:
        """Detect pre-X2-B Agent checkpoints without fabricating history."""

        for operation in ("load_plan", "load_critic_state"):
            loader = getattr(self._results, operation, None)
            if not callable(loader):
                continue
            if await loader(key) is not None:
                return True
        return False

    async def _load_pending_dead_letter(
        self,
        key,
    ) -> PendingDeadLetterHandoff | None:
        """Read the durable handoff before any terminal/recovery shortcut."""

        if self._dead_letter_handoff is not None:
            return await self._dead_letter_handoff.load_pending(key)
        legacy = self._pending_dead_letters.get(key)
        if legacy is None:
            return None
        request, attempt, error = legacy
        return PendingDeadLetterHandoff.from_request(
            request,
            attempt=attempt,
            error=error,
        )

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

    async def _retry_pending_dead_letter(
        self,
        key,
        current: TaskLifecycle,
        pending: PendingDeadLetterHandoff | None = None,
    ) -> WorkerOutcome:
        durable = pending is not None
        if pending is None:
            legacy = self._pending_dead_letters[key]
            request, attempt, error = legacy
        else:
            request = pending.to_request()
            attempt = pending.attempt
            error = pending.to_exception()
        if current.state is TaskStatusState.COMPLETED:
            raise RuntimeError("completed task cannot publish a pending dead-letter handoff")
        terminal = current
        if (
            current.state is not TaskStatusState.FAILED
            or current.stage is not TaskStage.DEAD_LETTERED
        ):
            terminal = current.fail(
                "分析失败，已进入人工处理队列",
                stage=TaskStage.DEAD_LETTERED,
            )
            # This closes the handoff-first crash boundary: a redelivery that
            # finds the durable handoff must durably finish the terminal state
            # before publishing or clearing that handoff.
            await self._lifecycle.save_lifecycle(terminal)
        if self._dead_letter is None:
            raise RuntimeError("dead-letter publisher is required")
        await self._dead_letter.publish(
            request,
            attempt=attempt,
            error=error,
        )
        # Remove the pending handoff only after publication succeeds.  The
        # durable delete is itself followed by a best-effort cache eviction
        # inside the Phase 8 adapter.
        if durable and self._dead_letter_handoff is not None:
            await self._dead_letter_handoff.clear_pending(key)
        else:
            self._pending_dead_letters.pop(key, None)
        await self._publish(
            key,
            TaskStatus.of(TaskStatus.State.FAILED, "分析失败，已进入人工处理队列"),
            TaskStage.DEAD_LETTERED,
        )
        return WorkerOutcome(
            WorkerDisposition.DEAD_LETTERED,
            terminal,
            error=error,
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
    def _profile_for(request: AnalysisRequest, profile: ModeProfile | None) -> ModeProfile:
        if profile is not None:
            return profile
        return mode_profile_for(request.mode)

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
            if isinstance(current, (BudgetExceededError, ValueError, TypeError, PermissionError, LookupError)):
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


AnalysisWorker = TaskWorker
AnalysisTaskWorker = TaskWorker


__all__ = [
    "AnalysisWorker",
    "AnalysisTaskWorker",
    "COMPLETED_TTL_SECONDS",
    "MAX_CAUSE_DEPTH",
    "TaskWorker",
    "WorkerDisposition",
    "WorkerOutcome",
]

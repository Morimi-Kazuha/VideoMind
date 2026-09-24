"""R3 worker composition over the existing R2 infrastructure and TaskWorker."""

from __future__ import annotations

import asyncio
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from dovideo.application import AgentCheckpointService, AgentLoopService, TaskEventDeliveryService
from dovideo.application.analysis_task_keys import goal_digest
from dovideo.application.ports.checkpoint import ContextCheckpointPort
from dovideo.application.ports.tasks import (
    AgentLoopEntryPort,
    TaskEventPublisherPort,
    TaskLifecyclePort,
)
from dovideo.application.value_objects import AnalysisRequest, TaskKey
from dovideo.application.worker import TaskWorker, WorkerOutcome
from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisEvidence,
    AnalysisResult,
    CriticResult,
    TaskEvent,
    TaskStage,
    TaskStatus,
    TaskStatusState,
    VideoContext,
)
from dovideo.infrastructure.persistence.dead_letter_handoff import (
    CheckpointDeadLetterHandoffStore,
)
from dovideo.infrastructure.persistence.sqlalchemy import FailedTaskRecord
from dovideo.infrastructure.persistence.task_lifecycle import (
    CheckpointTaskLifecycleStore,
)
from dovideo.infrastructure.redis_observability import RedisAgentTelemetry

from .celery_transport import (
    CeleryTransportSettings,
    RabbitMQDeadLetterPublisher,
    RabbitMQTopology,
)
from .r2_config import R2Infrastructure, create_r2_infrastructure


EVENT_LIST_PREFIX = "analysis:events"


class InvalidTransportEnvelope(ValueError):
    """Safe marker for a malformed broker body."""


class PoisonMessageUnresolved(RuntimeError):
    """Raised when a poison body cannot be durably recorded and DLQed."""


class TransportRecoveryError(RuntimeError):
    """Safe marker for a transport-side recovery redelivery."""


class DeliveryRetry(RuntimeError):
    """Safe marker for a TaskWorker-requested redelivery."""


_CURRENT_R3_REQUEST: ContextVar[AnalysisRequest | None] = ContextVar(
    "dovideo_r3_current_request",
    default=None,
)


class R3RequestContextCheckpoint(ContextCheckpointPort):
    """Bind a media-scoped checkpoint to the current delivery's goal.

    R2 deliberately stores reusable media context without a goal.  The
    existing AgentLoop validates that its input has the accepted request's
    goal, so this adapter restores that request-local field only at the
    transport composition boundary.  It never writes the goal back into the
    shared media checkpoint.
    """

    def __init__(self, delegate: ContextCheckpointPort) -> None:
        self.delegate = delegate

    async def load_context(self, media_id: int) -> VideoContext | None:
        context = await self.delegate.load_context(media_id)
        request = _CURRENT_R3_REQUEST.get()
        if context is None or request is None or request.media.media_id != media_id:
            return context
        if context.user_goal == request.goal:
            return context
        return context.model_copy(update={"user_goal": request.goal})

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        await self.delegate.save_context(media_id, context)

    async def load_chunks(self, media_id: int):
        return await self.delegate.load_chunks(media_id)

    async def save_chunks(self, media_id: int, chunks):
        await self.delegate.save_chunks(media_id, chunks)


def bind_r3_request(request: AnalysisRequest):
    """Set the request-local context used by one worker delivery."""

    if not isinstance(request, AnalysisRequest):
        raise TypeError("request must be an AnalysisRequest")
    return _CURRENT_R3_REQUEST.set(request)


def reset_r3_request(token: Any) -> None:
    """Restore the previous request after a worker delivery completes."""

    _CURRENT_R3_REQUEST.reset(token)


class _UnavailableAgentLoop(AgentLoopEntryPort):
    """Fail-closed production placeholder; it never silently uses local roles."""

    async def run(
        self,
        context: VideoContext,
        media_id: int | None = None,
        profile: Any | None = None,
    ) -> AgentState:
        del context, media_id, profile
        raise RuntimeError(
            "R3 worker AgentLoop provider is not configured; enable the later provider composition explicitly"
        )


class _R3ContextService:
    """Deterministic test-mode context boundary for the existing AgentLoop."""

    async def select_relevant(
        self,
        context: VideoContext,
        media_id: int | None = None,
    ) -> VideoContext:
        del media_id
        return context

    async def refine_for_critique(
        self,
        media_id: int | None,
        full_context: VideoContext,
        selected_context: VideoContext,
        critique: CriticResult | None,
    ) -> VideoContext:
        del media_id, full_context, critique
        return selected_context


class _R3Planner:
    async def plan(self, context: VideoContext, *, instruction: str = "") -> AgentPlan:
        del instruction
        return AgentPlan(
            understoodGoal=context.user_goal,
            tasks=(
                "读取已保存的时间窗口",
                "绑定时间戳证据",
                "生成结构化分析结果",
            ),
        )

    async def repair_plan(
        self,
        context: VideoContext,
        invalid_plan: AgentPlan,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        del invalid_plan
        return await self.plan(context, instruction=instruction)

    async def replan(
        self,
        context: VideoContext,
        current_plan: AgentPlan,
        critique: CriticResult,
        *,
        instruction: str = "",
    ) -> AgentPlan:
        del current_plan, critique
        return await self.plan(context, instruction=instruction)


class _R3Executor:
    async def execute(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        *,
        instruction: str = "",
    ) -> AnalysisResult:
        del plan, previous_critique, instruction
        segment = context.segments[0]
        content = segment.transcript or "timestamped video context"
        return AnalysisResult(
            title="R3 async transport validation",
            conclusions=("The existing AgentLoop produced a structured result from the saved context.",),
            evidence=(
                AnalysisEvidence(
                    timestampMs=segment.start_ms,
                    source="ASR",
                    content=content,
                    claim=content,
                ),
            ),
            suggestions=("Continue with the next productization phase only after R3 sign-off.",),
        )


class _R3Critic:
    async def critique(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult,
        *,
        instruction: str = "",
    ) -> CriticResult:
        del context, plan, result, instruction
        return CriticResult(passed=True, feedback=())


class R3DeterministicAgentLoop(AgentLoopEntryPort):
    """Explicit live-test wrapper around the existing ``AgentLoopService``.

    The wrapper only supplies deterministic failure/restart injection for the
    local R3 proof.  Successful work still enters the existing
    Planner/Executor/Critic ``AgentLoopService`` and its R2 checkpoints.
    """

    def __init__(self, delegate: AgentLoopEntryPort, redis_client: Any) -> None:
        self.delegate = delegate
        self.redis_client = redis_client

    async def run(
        self,
        context: VideoContext,
        media_id: int | None = None,
        profile: Any | None = None,
    ) -> AgentState:
        if media_id is None:
            raise ValueError("R3 deterministic AgentLoop requires media_id")
        digest = goal_digest(context.user_goal, None if profile is None else profile.mode)
        counter_key = f"r3:smoke:agent-invocations:{media_id}:{digest}"
        count = int(await asyncio.to_thread(self.redis_client.incr, counter_key))
        await asyncio.to_thread(self.redis_client.expire, counter_key, 24 * 60 * 60)
        goal = context.user_goal
        if "__R3_PERMANENT__" in goal:
            raise ValueError("deterministic permanent failure")
        if "__R3_TRANSIENT__" in goal and count <= 2:
            raise RuntimeError("deterministic transient failure")
        if "__R3_RESTART__" in goal and count == 1:
            delay = float(os.environ.get("DOVIDEO_R3_RESTART_SLEEP_SECONDS", "8"))
            await asyncio.sleep(max(1.0, min(delay, 30.0)))
        return await self.delegate.run(context, media_id=media_id, profile=profile)


class R3RedisTaskEventPublisher(TaskEventPublisherPort):
    """Persist existing TaskEvent frames for cross-process SSE consumers."""

    def __init__(
        self,
        redis_client: Any,
        lifecycle: TaskLifecyclePort,
        *,
        trace: Any | None = None,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        max_events: int = 256,
    ) -> None:
        self.redis_client = redis_client
        self.lifecycle = lifecycle
        self.trace = trace
        self.ttl_seconds = int(ttl_seconds)
        self.max_events = int(max_events)
        self.delivery = TaskEventDeliveryService()

    @classmethod
    def redis_key(cls, key: TaskKey) -> str:
        return f"{EVENT_LIST_PREFIX}:{key.media_id}:{goal_digest(key.goal, key.mode)}"

    async def publish(self, key: TaskKey, event: TaskEvent) -> None:
        if not isinstance(key, TaskKey) or not isinstance(event, TaskEvent):
            raise TypeError("event publication requires TaskKey and TaskEvent")
        # Run the existing status/event boundary first.  Its projection is an
        # in-process read-side optimization; the durable lifecycle remains the
        # authoritative cross-process status source.
        await self.delivery.publish(key, event)
        lifecycle = await self.lifecycle.load_lifecycle(key)
        attempt = 0 if lifecycle is None else lifecycle.attempt
        envelope = {
            "key": {
                "mediaId": key.media_id,
                "goal": key.goal,
                "mode": key.mode.value,
            },
            "event": event.model_dump(mode="json", by_alias=True),
            "attempt": attempt,
            "retryable": event.stage is TaskStage.RETRYING,
        }
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        redis_key = self.redis_key(key)
        await asyncio.to_thread(self.redis_client.rpush, redis_key, encoded)
        await asyncio.to_thread(self.redis_client.ltrim, redis_key, -self.max_events, -1)
        await asyncio.to_thread(self.redis_client.expire, redis_key, self.ttl_seconds)
        if self.trace is not None:
            try:
                await asyncio.to_thread(self.trace.record, key, event)
            except Exception:
                pass

    async def read(self, key: TaskKey) -> tuple[Any, ...]:
        values = await asyncio.to_thread(
            self.redis_client.lrange,
            self.redis_key(key),
            0,
            -1,
        )
        result: list[Any] = []
        from dovideo.application.task_lifecycle import TaskLifecycleEvent

        for raw in values or ():
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                value = json.loads(raw)
                event = TaskEvent.model_validate(value["event"])
                event_key = value["key"]
                parsed_key = TaskKey(
                    int(event_key["mediaId"]),
                    str(event_key["goal"]),
                    event_key.get("mode"),
                )
                if parsed_key != key:
                    continue
                result.append(
                    TaskLifecycleEvent(
                        key=parsed_key,
                        event=event,
                        attempt=int(value.get("attempt", 0)),
                        retryable=bool(value.get("retryable", False)),
                    )
                )
            except Exception:
                continue
        return tuple(result)


class R3StatusCheckpoint:
    """Status read port combining AgentCheckpoint result with task lifecycle."""

    def __init__(self, checkpoint: AgentCheckpointService, lifecycle: TaskLifecyclePort) -> None:
        self.checkpoint = checkpoint
        self.lifecycle = lifecycle

    async def load_result(self, key: TaskKey) -> AgentState | None:
        return await self.checkpoint.load_result(key)

    async def load_stage(self, key: TaskKey) -> TaskStage | None:
        current = await self.lifecycle.load_lifecycle(key)
        return None if current is None else current.stage


@dataclass(slots=True)
class R3WorkerRuntime:
    """One real Celery worker's R2-backed TaskWorker composition."""

    settings: CeleryTransportSettings
    infrastructure: R2Infrastructure
    lifecycle: CheckpointTaskLifecycleStore
    checkpoint: AgentCheckpointService
    events: R3RedisTaskEventPublisher
    dead_letter: RabbitMQDeadLetterPublisher
    handoff: CheckpointDeadLetterHandoffStore
    worker: TaskWorker
    topology: RabbitMQTopology
    initialized: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        settings: CeleryTransportSettings | None = None,
        infrastructure: R2Infrastructure | None = None,
        agent_loop: AgentLoopEntryPort | None = None,
    ) -> "R3WorkerRuntime":
        selected_settings = settings or CeleryTransportSettings.from_environment(
            require_production=True
        )
        selected_infrastructure = infrastructure or create_r2_infrastructure()
        checkpoint = AgentCheckpointService(selected_infrastructure.checkpoint_repository)
        context_checkpoint = R3RequestContextCheckpoint(checkpoint)
        lifecycle = CheckpointTaskLifecycleStore.from_environment(
            selected_infrastructure.checkpoint_repository,
            redis_client=selected_infrastructure.redis_client,
        )
        trace = RedisAgentTelemetry(selected_infrastructure.redis_client)
        events = R3RedisTaskEventPublisher(
            selected_infrastructure.redis_client,
            lifecycle,
            trace=trace,
        )
        dead_letter = RabbitMQDeadLetterPublisher(
            selected_settings,
            failed_task_store=selected_infrastructure.failed_task_store,
            redis_client=selected_infrastructure.redis_client,
        )
        handoff = CheckpointDeadLetterHandoffStore(
            selected_infrastructure.checkpoint_repository
        )
        task_lock = _r3_task_lock(selected_infrastructure)
        selected_agent_loop = agent_loop or _build_agent_loop(
            checkpoint,
            events,
            trace,
            selected_infrastructure.redis_client,
        )
        worker = TaskWorker(
            task_lock,
            selected_infrastructure.active_marker,
            lifecycle,
            context_checkpoint,
            selected_agent_loop,
            checkpoint,
            events=events,
            completion=selected_infrastructure.completion_marker,
            dead_letter=dead_letter,
            dead_letter_handoff=handoff,
        )
        return cls(
            settings=selected_settings,
            infrastructure=selected_infrastructure,
            lifecycle=lifecycle,
            checkpoint=checkpoint,
            events=events,
            dead_letter=dead_letter,
            handoff=handoff,
            worker=worker,
            topology=RabbitMQTopology(selected_settings),
        )

    async def initialize(self) -> None:
        if self.initialized:
            return
        await self.infrastructure.initialize()
        await asyncio.to_thread(self.topology.ensure)
        self.initialized = True

    async def process(self, request: AnalysisRequest) -> WorkerOutcome:
        await self.initialize()
        token = bind_r3_request(request)
        try:
            return await self.worker.handle(request)
        finally:
            reset_r3_request(token)

    async def record_poison(self, payload: Any, error: BaseException) -> None:
        await self.initialize()
        errors: list[BaseException] = []
        try:
            await asyncio.to_thread(self._record_poison_failure, payload, error)
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.dead_letter.publish_poison(payload, error)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise PoisonMessageUnresolved(
                "poison message durable record or DLQ publication failed"
            ) from errors[0]

    def _record_poison_failure(self, payload: Any, error: BaseException) -> None:
        media_id = 0
        mode = "GENERAL"
        if isinstance(payload, dict):
            candidate = payload.get("mediaId")
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                media_id = candidate
            mode = str(payload.get("mode", "GENERAL")).strip().upper() or "GENERAL"
            if mode not in {"GENERAL", "LEARNING", "REVIEW", "CREATION"}:
                mode = "GENERAL"
        self.infrastructure.failed_task_store.record(
            FailedTaskRecord(
                media_id=media_id,
                action="INVALID_TRANSPORT",
                mode=mode,
                content_hash="invalid-transport",
                user_goal="invalid transport envelope",
                attempt_count=0,
                error_type=type(error).__name__[:128],
                error_message="transport envelope validation failed",
                status="DEAD_LETTER_PENDING",
            )
        )

    async def close(self) -> None:
        self.infrastructure.close()


def _build_agent_loop(
    checkpoint: AgentCheckpointService,
    events: R3RedisTaskEventPublisher,
    trace: Any,
    redis_client: Any,
) -> AgentLoopEntryPort:
    if not _truthy(os.environ.get("DOVIDEO_R3_DETERMINISTIC_WORKER")):
        return _UnavailableAgentLoop()
    delegate = AgentLoopService(
        _R3ContextService(),
        _R3Planner(),
        _R3Executor(),
        checkpoint,
        events,
        trace,
        _R3Critic(),
    )
    return R3DeterministicAgentLoop(delegate, redis_client)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _r3_task_lock(infrastructure: R2Infrastructure) -> Any:
    """Use the configured R2 lock, with an explicit bounded test override."""

    raw = os.environ.get("DOVIDEO_R3_TASK_LOCK_TTL_MS")
    if raw is None or not raw.strip():
        return infrastructure.task_lock
    try:
        ttl_ms = int(raw)
    except ValueError as exc:
        raise ValueError("DOVIDEO_R3_TASK_LOCK_TTL_MS must be an integer") from exc
    if ttl_ms <= 0:
        raise ValueError("DOVIDEO_R3_TASK_LOCK_TTL_MS must be positive")
    from .redis import RedisTaskLock

    return RedisTaskLock(infrastructure.redis_client, ttl_ms=ttl_ms)


__all__ = [
    "DeliveryRetry",
    "EVENT_LIST_PREFIX",
    "InvalidTransportEnvelope",
    "PoisonMessageUnresolved",
    "R3RequestContextCheckpoint",
    "R3DeterministicAgentLoop",
    "R3RedisTaskEventPublisher",
    "R3StatusCheckpoint",
    "R3WorkerRuntime",
    "TransportRecoveryError",
    "bind_r3_request",
    "reset_r3_request",
]

"""Async Java-compatible Agent checkpoint service for Phase 8B.

The application service owns media-vs-goal key selection and model typing;
the synchronous 8A repository owns durable-first/cache-aside semantics.  All
repository calls run through ``asyncio.to_thread`` so a file-backed SQLite
adapter cannot block the event loop.  The repository's own lock makes the
injected SQLite connection safe for those worker-thread calls.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from dovideo.domain import (
    AgentFeedback,
    AgentPlan,
    AgentState,
    AnalysisMode,
    AnalysisResult,
    TaskStage,
    VideoChunk,
    VideoContext,
)
from dovideo.domain._base import DomainModel

from .analysis_task_keys import goal_digest
from .errors import AgentFeedbackPersistenceError
from .ports.checkpoint import (
    AgentCheckpointPort,
    AnalysisStatusCheckpointPort,
    ContextCheckpointPort,
    ModelRoutingCheckpointPort,
    ToolCallCheckpointPort,
)
from .model_routing import ModelRoutingDecision
from .tool_state import DurableToolCallState, DurableToolStateLedger
from .value_objects import TaskKey


T = TypeVar("T")


class RevisionCheckpoint(DomainModel):
    """Internal durable state for Java's staged plan revision workflow."""

    plan: AgentPlan | None = None
    applied: bool = False


class AgentCheckpointService(
    AgentCheckpointPort,
    ContextCheckpointPort,
    AnalysisStatusCheckpointPort,
    ToolCallCheckpointPort,
    ModelRoutingCheckpointPort,
):
    """Persist typed media and goal checkpoints through an 8A repository."""

    def __init__(
        self,
        repository: Any | None = None,
        *,
        checkpoint_repository: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository if repository is not None else checkpoint_repository
        if self._repository is None:
            raise ValueError("a checkpoint repository is required")
        # Public names help composition roots inspect the injected boundary.
        self.repository = self._repository
        self.checkpoint_repository = self._repository
        self._clock = clock

    # ------------------------------------------------------------------
    # Pure Java key policy
    # ------------------------------------------------------------------
    @staticmethod
    def checkpoint_key(media_id: int) -> str:
        return f"agent:checkpoint:{media_id}"

    @staticmethod
    def goal_key(media_id: int, goal: str, mode: AnalysisMode | str | None = None) -> str:
        return f"{AgentCheckpointService.checkpoint_key(media_id)}:goal:{goal_digest(goal, mode)}"

    @staticmethod
    def media_checkpoint(field: str) -> str:
        return f"media:{field}"

    @staticmethod
    def goal_checkpoint(
        goal: str,
        mode: AnalysisMode | str | None,
        field: str,
    ) -> str:
        return f"goal:{goal_digest(goal, mode)}:{field}"

    @staticmethod
    def feedback_key(media_id: int) -> str:
        return f"agent:feedback:{media_id}"

    @staticmethod
    def goal_index_key(media_id: int) -> str:
        return f"agent:checkpoint:{media_id}:goals"

    @staticmethod
    def revision_key(
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> str:
        return f"{AgentCheckpointService.goal_key(media_id, goal, mode)}:revision"

    @staticmethod
    def revision_checkpoint(
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> str:
        return f"revision:{goal_digest(goal, mode)}"

    @staticmethod
    def tool_checkpoint(
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> str:
        """Independent checkpoint row for the X1-D1 tool ledger."""

        return f"{AgentCheckpointService.goal_checkpoint(goal, mode, 'toolState')}"

    @staticmethod
    def tool_cache_key(
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> str:
        # Keep the tool ledger out of the goal hash's primary ``stage`` field;
        # tool-state cache writes must never rewrite the frozen R0-R3 stage.
        return f"{AgentCheckpointService.goal_key(media_id, goal, mode)}:tools"

    @staticmethod
    def model_routing_checkpoint(
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> str:
        """Independent checkpoint name for the temporary J1 route fact."""

        return f"{AgentCheckpointService.goal_checkpoint(goal, mode, 'modelRouting')}"

    @staticmethod
    def model_routing_cache_key(
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> str:
        """Hot-cache key for the temporary J1 route fact."""

        return f"{AgentCheckpointService.goal_key(media_id, goal, mode)}:model-routing"

    # Java migration spellings.
    checkpointKey = checkpoint_key
    goalKey = goal_key
    mediaCheckpoint = media_checkpoint
    goalCheckpoint = goal_checkpoint
    feedbackKey = feedback_key
    goalIndexKey = goal_index_key
    revisionKey = revision_key
    revisionCheckpoint = revision_checkpoint
    toolCheckpoint = tool_checkpoint
    toolCacheKey = tool_cache_key
    modelRoutingCheckpoint = model_routing_checkpoint
    modelRoutingCacheKey = model_routing_cache_key

    # ------------------------------------------------------------------
    # Async adapter bridge
    # ------------------------------------------------------------------
    async def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self._repository, operation, None)
        if not callable(method):
            raise TypeError(f"checkpoint repository has no {operation} operation")
        # to_thread also works for in-memory adapters and keeps this service's
        # contract safe if the composition root later swaps in file-backed DB.
        result = await asyncio.to_thread(method, *args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    @classmethod
    def _goal_coordinates(
        cls,
        key: TaskKey,
        field: str,
    ) -> tuple[int, str, str, str]:
        mode = key.mode
        return (
            key.media_id,
            cls.goal_checkpoint(key.goal, mode, field),
            cls.goal_key(key.media_id, key.goal, mode),
            field,
        )

    @classmethod
    def _stage_checkpoint(cls, key: TaskKey) -> str:
        return cls.goal_checkpoint(key.goal, key.mode, "stage")

    def _cache_object(self) -> Any | None:
        # Do not use ``or`` here: an injected cache may intentionally be a
        # false-y proxy while still implementing the hot-cache protocol.
        cache = getattr(self._repository, "cache", None)
        return cache if cache is not None else getattr(
            self._repository, "hot_cache", None
        )

    async def _cache_call(self, names: tuple[str, ...], *args: Any) -> Any:
        cache = self._cache_object()
        if cache is None:
            raise TypeError("checkpoint repository has no hot cache")
        for name in names:
            method = getattr(cache, name, None)
            if callable(method):
                result = await asyncio.to_thread(method, *args)
                if inspect.isawaitable(result):
                    return await result
                return result
        raise TypeError(f"hot cache has no {names[0]} operation")

    async def _remember_goal_key(self, media_id: int, redis_key: str) -> None:
        """Best-effort Java ``rememberGoalKey`` set/index update."""

        try:
            await self._cache_call(
                ("set_add", "sadd"), self.goal_index_key(media_id), redis_key
            )
            await self._cache_call(
                ("expire",), self.goal_index_key(media_id), 7 * 24 * 60 * 60
            )
        except Exception:
            # The index accelerates media deletion only; durable checkpoint
            # writes must remain successful when the cache is unavailable.
            pass

    async def _cache_delete_key(self, redis_key: str) -> None:
        try:
            await self._cache_call(("delete_key", "delete"), redis_key)
        except Exception:
            pass

    async def _cache_members(self, redis_key: str) -> tuple[str, ...]:
        values = await self._cache_call(("set_members", "smembers"), redis_key)
        if values is None:
            return ()
        return tuple(str(value) for value in values)

    # ------------------------------------------------------------------
    # Content-level context/chunks (cross-goal, media scoped)
    # ------------------------------------------------------------------
    async def load_context(self, media_id: int) -> VideoContext | None:
        return await self._call(
            "read",
            media_id,
            self.media_checkpoint("context"),
            self.checkpoint_key(media_id),
            "context",
            VideoContext,
        )

    async def save_context(self, media_id: int, context: VideoContext) -> None:
        reusable = VideoContext(
            source=context.source,
            user_goal="",
            segments=tuple(context.segments),
            observations=tuple(context.observations),
            source_revision=context.source_revision,
            provenance_version=context.provenance_version,
        )
        await self._call(
            "write",
            media_id,
            self.media_checkpoint("context"),
            self.media_checkpoint("stage"),
            self.checkpoint_key(media_id),
            "context",
            TaskStage.CONTEXT_COMPLETED,
            reusable,
        )

    async def load_chunks(self, media_id: int) -> tuple[VideoChunk, ...] | None:
        chunks = await self._call(
            "read",
            media_id,
            self.media_checkpoint("chunks"),
            self.checkpoint_key(media_id),
            "chunks",
            tuple[VideoChunk, ...],
        )
        return None if chunks is None else tuple(chunks)

    async def save_chunks(self, media_id: int, chunks: tuple[VideoChunk, ...]) -> None:
        copied = tuple(chunks)
        await self._call(
            "write",
            media_id,
            self.media_checkpoint("chunks"),
            self.media_checkpoint("stage"),
            self.checkpoint_key(media_id),
            "chunks",
            TaskStage.CHUNKS_COMPLETED,
            copied,
        )

    # ------------------------------------------------------------------
    # Goal-level plan/draft/Critic/result/status methods
    # ------------------------------------------------------------------
    async def load_plan(self, key: TaskKey) -> AgentPlan | None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "plan")
        return await self._call("read", media_id, checkpoint, redis_key, field, AgentPlan)

    async def save_plan(self, key: TaskKey, plan: AgentPlan) -> None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "plan")
        await self._call(
            "write",
            media_id,
            checkpoint,
            self._stage_checkpoint(key),
            redis_key,
            field,
            TaskStage.PLAN_COMPLETED,
            plan,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def load_critic_state(self, key: TaskKey) -> AgentState | None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "criticState")
        return await self._call("read", media_id, checkpoint, redis_key, field, AgentState)

    async def save_execution_state(self, key: TaskKey, state: AgentState) -> None:
        # Java stores the Executor draft in criticState so the next consumer
        # can resume directly at Critic without a second Executor call.
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "criticState")
        await self._call(
            "write",
            media_id,
            checkpoint,
            self._stage_checkpoint(key),
            redis_key,
            field,
            TaskStage.EXECUTOR_COMPLETED,
            state,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def save_critic_state(self, key: TaskKey, state: AgentState) -> None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "criticState")
        stage = (
            TaskStage.CRITIC_PASSED
            if state.critique is not None and state.critique.passed
            else TaskStage.CRITIC_RETRY_REQUIRED
        )
        await self._call(
            "write",
            media_id,
            checkpoint,
            self._stage_checkpoint(key),
            redis_key,
            field,
            stage,
            state,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def load_result(self, key: TaskKey) -> AgentState | None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "result")
        return await self._call("read", media_id, checkpoint, redis_key, field, AgentState)

    async def save_result(self, key: TaskKey, state: AgentState) -> None:
        media_id, checkpoint, redis_key, field = self._goal_coordinates(key, "result")
        stage = (
            TaskStage.ANALYSIS_COMPLETED
            if state.critique is not None and state.critique.passed
            else TaskStage.ANALYSIS_COMPLETED_WITH_WARNINGS
        )
        await self._call(
            "write",
            media_id,
            checkpoint,
            self._stage_checkpoint(key),
            redis_key,
            field,
            stage,
            state,
        )
        await self._remember_goal_key(media_id, redis_key)

    async def load_stage(self, key: TaskKey) -> TaskStage | None:
        media_id = key.media_id
        return await self._call(
            "read_stage",
            media_id,
            self._stage_checkpoint(key),
            self.goal_key(key.media_id, key.goal, key.mode),
        )

    async def save_stage(self, key: TaskKey, stage: TaskStage) -> None:
        """Persist only a goal stage through the repository's stage path."""

        await self._call(
            "write_stage",
            key.media_id,
            self._stage_checkpoint(key),
            self.goal_key(key.media_id, key.goal, key.mode),
            stage,
        )
        await self._remember_goal_key(
            key.media_id, self.goal_key(key.media_id, key.goal, key.mode)
        )

    # ------------------------------------------------------------------
    # X1-D1 durable tool ledger (same repository, independent namespace)
    # ------------------------------------------------------------------
    async def load_tool_state(self, key: TaskKey) -> DurableToolStateLedger | None:
        """Load the durable tool ledger without touching the goal stage row."""

        value = await self._call(
            "read",
            key.media_id,
            self.tool_checkpoint(key.goal, key.mode),
            self.tool_cache_key(key.media_id, key.goal, key.mode),
            "toolState",
            DurableToolStateLedger,
        )
        if value is None:
            return None
        return (
            value
            if isinstance(value, DurableToolStateLedger)
            else DurableToolStateLedger.model_validate(value)
        )

    async def save_tool_state(
        self,
        key: TaskKey,
        state: DurableToolStateLedger,
    ) -> None:
        """Persist one complete ledger snapshot durably before cache warming."""

        if not isinstance(key, TaskKey):
            raise TypeError("tool checkpoint key must be a TaskKey")
        if not isinstance(state, DurableToolStateLedger):
            state = DurableToolStateLedger.model_validate(state)
        if state.task_key != key:
            raise ValueError("tool ledger task identity does not match checkpoint key")
        await self._call(
            "write_standalone",
            key.media_id,
            self.tool_checkpoint(key.goal, key.mode),
            self.tool_cache_key(key.media_id, key.goal, key.mode),
            "toolState",
            # This stage belongs only to the independent tool-state row.  It
            # is deliberately not written to the goal stage checkpoint.
            TaskStage.EXECUTOR_STARTED,
            state,
        )
        await self._remember_goal_key(
            key.media_id,
            self.tool_cache_key(key.media_id, key.goal, key.mode),
        )

    async def save_tool_call_state(self, key: TaskKey, state: Any) -> None:
        """Convenience upsert for adapters that advance one logical call."""

        if not isinstance(state, DurableToolCallState):
            state = DurableToolCallState.model_validate(state)
        current = await self.load_tool_state(key)
        if current is None:
            current = DurableToolStateLedger(task_key=key)
        await self.save_tool_state(key, current.with_record(state))

    async def load_tool_calls(self, key: TaskKey) -> tuple[Any, ...]:
        """Compatibility reader for focused recovery adapters/tests."""

        state = await self.load_tool_state(key)
        return () if state is None else state.records

    # ------------------------------------------------------------------
    # J1-B temporary stable route recovery (not an X2 event)
    # ------------------------------------------------------------------
    async def load_model_routing(
        self,
        key: TaskKey,
    ) -> ModelRoutingDecision | None:
        """Load one immutable route decision from its independent namespace."""

        if not isinstance(key, TaskKey):
            raise TypeError("model routing checkpoint key must be a TaskKey")
        value = await self._call(
            "read",
            key.media_id,
            self.model_routing_checkpoint(key.goal, key.mode),
            self.model_routing_cache_key(key.media_id, key.goal, key.mode),
            "modelRouting",
            ModelRoutingDecision,
        )
        if value is None:
            return None
        return (
            value
            if isinstance(value, ModelRoutingDecision)
            else ModelRoutingDecision.model_validate(value)
        )

    async def save_model_routing(
        self,
        key: TaskKey,
        decision: ModelRoutingDecision,
    ) -> None:
        """Durably save a route before the first lane-specific model call."""

        if not isinstance(key, TaskKey):
            raise TypeError("model routing checkpoint key must be a TaskKey")
        if not isinstance(decision, ModelRoutingDecision):
            decision = ModelRoutingDecision.model_validate(decision)
        await self._call(
            "write_standalone",
            key.media_id,
            self.model_routing_checkpoint(key.goal, key.mode),
            self.model_routing_cache_key(key.media_id, key.goal, key.mode),
            "modelRouting",
            # This is an independent route checkpoint.  It must not advance
            # the frozen goal-stage row or create a new public event.
            TaskStage.AGENT_LOOP,
            decision,
        )
        await self._remember_goal_key(
            key.media_id,
            self.model_routing_cache_key(key.media_id, key.goal, key.mode),
        )

    # ------------------------------------------------------------------
    # Staged plan revision lifecycle (Java AgentCheckpointService)
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_revision_plan(plan: AgentPlan | Any | None) -> AgentPlan | None:
        if plan is None or isinstance(plan, AgentPlan):
            return plan
        return AgentPlan.model_validate(plan)

    async def stage_revision(
        self,
        media_id: int,
        goal: str,
        plan: AgentPlan | Any | None = None,
        mode: AnalysisMode | str | None = None,
    ) -> None:
        """Durably stage a corrected plan without changing active state."""

        # Accept Java's (mediaId, goal, mode, plan) positional ordering too.
        if isinstance(plan, (AnalysisMode, str)) and (
            mode is None or isinstance(mode, (AgentPlan, dict))
        ):
            plan, mode = mode, plan
        resolved_plan = self._coerce_revision_plan(plan)
        revision_checkpoint = self.revision_checkpoint(goal, mode)
        revision_key = self.revision_key(media_id, goal, mode)
        await self._call(
            "write_standalone",
            media_id,
            revision_checkpoint,
            revision_key,
            "revision",
            TaskStage.REVISION_PENDING,
            RevisionCheckpoint(plan=resolved_plan, applied=False),
        )
        await self._remember_goal_key(media_id, revision_key)

    async def begin_staged_revision(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> bool:
        """Apply a staged revision idempotently and safely retry partial work."""

        revision_checkpoint = self.revision_checkpoint(goal, mode)
        revision_key = self.revision_key(media_id, goal, mode)
        revision = await self._call(
            "read",
            media_id,
            revision_checkpoint,
            revision_key,
            "revision",
            RevisionCheckpoint,
        )
        if revision is None:
            return False
        if not isinstance(revision, RevisionCheckpoint):
            revision = RevisionCheckpoint.model_validate(revision)
        if revision.applied:
            return True

        goal_key = self.goal_key(media_id, goal, mode)
        # Remove plan/draft/result/stage before applying the corrected plan.
        # The revision row lives under revision:<digest>, so it is retained.
        await self._call(
            "delete_prefix",
            media_id,
            self.goal_checkpoint(goal, mode, ""),
            redis_key=goal_key,
        )
        if revision.plan is not None:
            await self.save_plan(TaskKey(media_id, goal, AnalysisMode.from_nullable(mode)), revision.plan)
        await self._call(
            "write_standalone",
            media_id,
            revision_checkpoint,
            revision_key,
            "revision",
            TaskStage.REVISION_APPLIED,
            RevisionCheckpoint(plan=revision.plan, applied=True),
        )
        await self._remember_goal_key(media_id, revision_key)
        return True

    async def complete_staged_revision(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> None:
        await self._call(
            "delete",
            media_id,
            self.revision_checkpoint(goal, mode),
            self.revision_key(media_id, goal, mode),
        )

    async def cancel_staged_revision(
        self,
        media_id: int,
        goal: str,
        mode: AnalysisMode | str | None = None,
    ) -> None:
        await self.complete_staged_revision(media_id, goal, mode)

    # ------------------------------------------------------------------
    # Hot-only user feedback and failure metadata
    # ------------------------------------------------------------------
    async def save_feedback(self, feedback: AgentFeedback | Any) -> None:
        if not isinstance(feedback, AgentFeedback):
            feedback = AgentFeedback.model_validate(feedback)
        normalized = feedback.normalized(clock=self._clock)
        payload = normalized.model_dump_json(by_alias=True)
        key = self.feedback_key(normalized.media_id)
        try:
            await self._cache_call(("right_push", "rpush", "rightPush"), key, payload)
            await self._cache_call(("trim", "ltrim"), key, -200, -1)
            await self._cache_call(("expire",), key, 30 * 24 * 60 * 60)
        except Exception as exc:
            raise AgentFeedbackPersistenceError("保存 Agent 用户反馈失败") from exc

    async def load_feedback(self, media_id: int) -> tuple[AgentFeedback, ...]:
        try:
            values = await self._cache_call(
                ("list_range", "lrange", "range"),
                self.feedback_key(media_id),
                0,
                -1,
            )
        except Exception as exc:
            raise AgentFeedbackPersistenceError("读取 Agent 用户反馈失败") from exc
        if values is None:
            return ()
        loaded: list[AgentFeedback] = []
        for value in values:
            try:
                if isinstance(value, (bytes, bytearray)):
                    value = bytes(value).decode("utf-8")
                loaded.append(AgentFeedback.model_validate_json(value))
            except Exception:
                # Java skips one malformed list item while retaining others.
                continue
        return tuple(loaded)

    async def save_failure(
        self,
        media_id: int,
        goal: str,
        failed_stage: TaskStage | str,
        error: BaseException,
        mode: AnalysisMode | str | None = None,
    ) -> None:
        """Persist a FAILED stage and best-effort non-secret cache metadata."""

        # Also accept Java's (mediaId, goal, mode, failedStage, error) order.
        if (
            not isinstance(failed_stage, TaskStage)
            and isinstance(error, TaskStage)
            and isinstance(mode, BaseException)
        ):
            failed_stage, error, mode = error, mode, failed_stage
        stage = (
            failed_stage
            if isinstance(failed_stage, TaskStage)
            else TaskStage.from_value(str(failed_stage))
        )
        if stage is None:
            raise ValueError(f"unknown failed stage: {failed_stage}")
        if not isinstance(error, BaseException):
            raise TypeError("error must be an exception")
        key = self.goal_key(media_id, goal, mode)
        await self._call(
            "write_stage",
            media_id,
            self.goal_checkpoint(goal, mode, "stage"),
            key,
            TaskStage.FAILED,
        )
        cache = self._cache_object()
        if cache is not None:
            try:
                await self._cache_call(("set_hash", "hset", "put"), key, "failedStage", stage.value)
                await self._cache_call(
                    ("set_hash", "hset", "put"),
                    key,
                    "errorType",
                    error.__class__.__name__,
                )
                await self._cache_call(("expire",), key, 7 * 24 * 60 * 60)
            except Exception:
                pass
        await self._remember_goal_key(media_id, key)

    async def delete_media(self, media_id: int) -> None:
        """Delete durable media rows and all known hot media/goal keys."""

        keys = [
            self.checkpoint_key(media_id),
            self.feedback_key(media_id),
            self.goal_index_key(media_id),
        ]
        try:
            keys.extend(await self._cache_members(self.goal_index_key(media_id)))
        except Exception:
            # Durable deletion still proceeds when the index/cache is down.
            pass
        await self._call("delete_media", media_id, tuple(dict.fromkeys(keys)))

    # Java overload-free Python aliases and migration spellings.
    loadContext = load_context
    saveContext = save_context
    loadChunks = load_chunks
    saveChunks = save_chunks
    loadPlan = load_plan
    savePlan = save_plan
    loadCriticState = load_critic_state
    saveCriticState = save_critic_state
    loadExecutionState = load_critic_state
    saveExecutionState = save_execution_state
    loadResult = load_result
    saveResult = save_result
    loadStage = load_stage
    saveStage = save_stage
    loadToolState = load_tool_state
    saveToolState = save_tool_state
    saveToolCallState = save_tool_call_state
    loadToolCalls = load_tool_calls
    loadToolLedger = load_tool_state
    saveToolLedger = save_tool_state
    saveToolCall = save_tool_call_state
    loadModelRouting = load_model_routing
    saveModelRouting = save_model_routing
    stageRevision = stage_revision
    beginStagedRevision = begin_staged_revision
    completeStagedRevision = complete_staged_revision
    cancelStagedRevision = cancel_staged_revision
    saveFeedback = save_feedback
    loadFeedback = load_feedback
    saveFailure = save_failure
    deleteMedia = delete_media


# A concise alias is convenient for composition roots that call the service a
# checkpoint adapter rather than retaining the Java service name.
CheckpointService = AgentCheckpointService


__all__ = ["AgentCheckpointService", "CheckpointService", "RevisionCheckpoint"]

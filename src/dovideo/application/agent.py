"""Controlled Agent application boundary for Phase 7A--7F.

The service keeps role calls and persistence behind the narrow application
ports.  The 7F budget seam constrains async role/context calls with the active
deadline while leaving provider-reported token/cost accounting injectable.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import nullcontext
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from dovideo.domain import (
    AgentPlan,
    AgentState,
    AnalysisMode,
    AnalysisResult,
    BudgetUsage,
    CriticResult,
    ModeProfile,
    TaskEvent,
    TaskStage,
    TaskStatus,
    TaskStatusState,
    VideoContext,
)
from dovideo.domain.budget import AgentBudgetConfig

from .agent_policy import *  # noqa: F401,F403
from .agent_policy import __all__ as _POLICY_EXPORTS
from .evidence import (
    bind_evidence_provenance as _bind_evidence_provenance,
    enforce_evidence_bounds as _enforce_evidence_bounds,
)
from .errors import BudgetExceededError, DeadlineExceededError
from .execution_record import (
    DurableAgentExecutionRecord,
    ExecutionEventType,
    ExecutionRecordConflictError,
    ExecutionRecordService,
    ExecutionRecordStatus,
)
from .execution_budget import AgentExecutionBudget
from .ports.ai import CriticPort, ExecutorPort, ExecutorTurnPort, PlannerPort
from .ports.checkpoint import AgentCheckpointPort, ToolCallCheckpointPort
from .ports.observability import AgentBudgetUsagePort, TelemetryPort
from .ports.tasks import TaskEventPublisherPort
from .ports.tools import ToolExecutionContext, ToolExecutorPort
from .tool_contracts import (
    AgentRole,
    ExecutorTurn,
    ExecutorTurnKind,
    ModelToolRequest,
    ToolCall,
    ToolName,
    ToolResult,
    ToolResultStatus,
    PolicyDecision,
    ToolPolicyDecision,
    ToolPolicyReasonCode,
)
from .tool_policy import (
    DEFAULT_TOOL_CALLS_PER_ROUND,
    DEFAULT_TOTAL_TOOL_CALLS,
    ToolPolicy,
    ToolPolicyContext,
)
from .tool_state import (
    DurableToolCallState,
    DurableToolExecutionState,
    DurableToolStateLedger,
    ToolRecoveryIdentityMismatchError,
    canonical_tool_arguments_digest,
    empty_tool_state_ledger,
)
from .value_objects import TaskKey


_BUDGET_TERMINATION_RECORDED: ContextVar[bool] = ContextVar(
    "dovideo_agent_budget_termination_recorded", default=False
)


class ToolTurnBudgetExceededError(RuntimeError):
    """A tool-aware Executor kept requesting tools after they were disabled."""


class ToolCallingDisabledWithInFlightStateError(RuntimeError):
    """The rollout flag was disabled while a durable X1 turn was incomplete."""


@dataclass(slots=True)
class _ToolRequestBudget:
    """Runtime-only request-attempt counters for one Agent run."""

    per_round_limit: int
    total_limit: int
    total: int = 0
    round_number: int | None = None
    round_total: int = 0

    def begin_round(self, round_number: int) -> None:
        if self.round_number == round_number:
            return
        self.round_number = round_number
        self.round_total = 0

    def consume(self, round_number: int) -> tuple[int, int, str]:
        self.begin_round(round_number)
        previous_round_total = self.round_total
        previous_total = self.total
        self.round_total += 1
        self.total += 1
        return previous_round_total, previous_total, f"tool-call-{self.total}"

    def has_capacity(self) -> bool:
        return (
            self.round_total < self.per_round_limit
            and self.total < self.total_limit
        )

    def hydrate_from_ledger(
        self,
        ledger: DurableToolStateLedger,
        round_number: int,
    ) -> None:
        """Derive counters from durable logical requests without double count."""

        self.total = max(self.total, ledger.request_count)
        self.round_number = round_number
        self.round_total = ledger.count_for_round(round_number)


@dataclass(slots=True)
class _ToolRoundOutcome:
    """Tool-aware result plus a post-draft durable consumption callback."""

    result: AnalysisResult
    finalize_consumption: Callable[[], Awaitable[None]] | None = None


def _validate_tool_request_limit(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


class _NullTelemetry:
    """No-op fallback so policy tests need not construct an adapter."""

    def increment(self, metric: str, amount: int = 1, **_: Any) -> None:
        del metric, amount


class AgentLoopService:
    """Resolve one bounded plan and produce one persisted Executor draft.

    ``resolve_plan`` mirrors the Java private helper's precedence and repair
    behavior.  ``execute_round`` mirrors only the Java Executor checkpoint
    portion; it intentionally returns a state with ``critique=None`` for the
    later Critic slice to consume.
    """

    # Java exposes this as AgentLoopService.BudgetExceededException.
    BudgetExceededError = BudgetExceededError
    BudgetExceededException = BudgetExceededError
    BudgetExceeded = BudgetExceededError

    def __init__(
        self,
        context_service: Any | None = None,
        planner: PlannerPort | None = None,
        executor: ExecutorPort | None = None,
        checkpoint: AgentCheckpointPort | None = None,
        event_publisher: TaskEventPublisherPort | None = None,
        telemetry: TelemetryPort | None = None,
        critic: CriticPort | None = None,
        *,
        long_context: Any | None = None,
        long_context_service: Any | None = None,
        long_video_context_service: Any | None = None,
        planner_port: PlannerPort | None = None,
        executor_port: ExecutorPort | None = None,
        checkpoint_service: AgentCheckpointPort | None = None,
        task_event_publisher: TaskEventPublisherPort | None = None,
        events: TaskEventPublisherPort | None = None,
        critic_port: CriticPort | None = None,
        budget_config: AgentBudgetConfig | Mapping[str, Any] | None = None,
        budget: AgentBudgetConfig | Mapping[str, Any] | None = None,
        execution_budget: AgentExecutionBudget | Any | None = None,
        agent_execution_budget: AgentExecutionBudget | Any | None = None,
        usage_source: AgentBudgetUsagePort | Any | None = None,
        budget_usage: AgentBudgetUsagePort | Any | None = None,
        usage: AgentBudgetUsagePort | Any | None = None,
        budget_usage_source: AgentBudgetUsagePort | Any | None = None,
        usage_port: AgentBudgetUsagePort | Any | None = None,
        usage_tracker: AgentBudgetUsagePort | Any | None = None,
        executor_turn: ExecutorTurnPort | None = None,
        executor_turn_port: ExecutorTurnPort | None = None,
        tool_executor: ToolExecutorPort | None = None,
        tool_executor_port: ToolExecutorPort | None = None,
        tool_checkpoint: ToolCallCheckpointPort | None = None,
        tool_checkpoint_port: ToolCallCheckpointPort | None = None,
        tool_call_checkpoint: ToolCallCheckpointPort | None = None,
        tool_call_checkpoint_port: ToolCallCheckpointPort | None = None,
        durable_tool_checkpoint: ToolCallCheckpointPort | None = None,
        tool_policy: ToolPolicy | None = None,
        tool_calling_enabled: bool | None = None,
        tool_request_limit_per_round: int = DEFAULT_TOOL_CALLS_PER_ROUND,
        tool_request_limit_total: int = DEFAULT_TOTAL_TOOL_CALLS,
        max_tool_requests_per_round: int | None = None,
        max_tool_requests_total: int | None = None,
        monotonic: Any | None = None,
        monotonic_clock: Any | None = None,
        clock: Any | None = None,
        execution_record: ExecutionRecordService | None = None,
        execution_recorder: ExecutionRecordService | None = None,
        execution_record_service: ExecutionRecordService | None = None,
    ) -> None:
        """Build the service from ports.

        The first six positional parameters follow the natural Python order
        (context, planner, executor, checkpoint, events, telemetry).  Named
        aliases retain the terminology used by the Java service and existing
        application composition code.
        """

        context_service = (
            context_service
            if context_service is not None
            else long_context_service
            if long_context_service is not None
            else long_video_context_service
            if long_video_context_service is not None
            else long_context
        )
        planner = planner if planner is not None else planner_port
        executor = executor if executor is not None else executor_port
        checkpoint = checkpoint if checkpoint is not None else checkpoint_service
        event_publisher = (
            event_publisher
            if event_publisher is not None
            else task_event_publisher
            if task_event_publisher is not None
            else events
        )
        critic = critic if critic is not None else critic_port
        if budget_config is None:
            budget_config = budget
        if execution_budget is None:
            execution_budget = agent_execution_budget
        if usage_source is None:
            usage_source = (
                budget_usage
                if budget_usage is not None
                else usage
                if usage is not None
                else budget_usage_source
                if budget_usage_source is not None
                else usage_port
                if usage_port is not None
                else usage_tracker
            )
        if monotonic is None:
            monotonic = monotonic_clock

        execution_record_service = (
            execution_record_service
            if execution_record_service is not None
            else execution_recorder
            if execution_recorder is not None
            else execution_record
        )

        executor_turn = (
            executor_turn if executor_turn is not None else executor_turn_port
        )
        tool_executor = (
            tool_executor if tool_executor is not None else tool_executor_port
        )
        tool_checkpoint = (
            tool_checkpoint
            if tool_checkpoint is not None
            else tool_checkpoint_port
            if tool_checkpoint_port is not None
            else tool_call_checkpoint
            if tool_call_checkpoint is not None
            else tool_call_checkpoint_port
            if tool_call_checkpoint_port is not None
            else durable_tool_checkpoint
        )
        if max_tool_requests_per_round is not None:
            tool_request_limit_per_round = max_tool_requests_per_round
        if max_tool_requests_total is not None:
            tool_request_limit_total = max_tool_requests_total

        # Accommodate the equally natural role-first positional composition:
        # AgentLoopService(planner, executor, context_service, ...).
        if (
            context_service is not None
            and planner is not None
            and executor is not None
            and hasattr(context_service, "plan")
            and hasattr(planner, "execute")
            and hasattr(executor, "select_relevant")
        ):
            context_service, planner, executor = executor, context_service, planner

        if context_service is None:
            raise TypeError("context_service is required")
        if planner is None:
            raise TypeError("planner is required")
        if executor is None:
            raise TypeError("executor is required")

        self._context_service = context_service
        self._long_context = context_service
        self._planner = planner
        self._executor = executor
        self._checkpoint = checkpoint
        self._event_publisher = event_publisher
        self._events = event_publisher
        self._critic = critic
        if (executor_turn is None) != (tool_executor is None):
            raise TypeError(
                "executor_turn and tool_executor must be configured together"
            )
        self._executor_turn = executor_turn
        self._tool_executor = tool_executor
        self._tool_checkpoint = tool_checkpoint
        if tool_calling_enabled is not None and not isinstance(
            tool_calling_enabled, bool
        ):
            raise TypeError("tool_calling_enabled must be boolean or None")
        self._tool_calling_enabled = tool_calling_enabled
        self._tool_policy = tool_policy if tool_policy is not None else ToolPolicy()
        self._tool_request_limit_per_round = _validate_tool_request_limit(
            tool_request_limit_per_round,
            "tool_request_limit_per_round",
        )
        self._tool_request_limit_total = _validate_tool_request_limit(
            tool_request_limit_total,
            "tool_request_limit_total",
        )
        self._telemetry = telemetry if telemetry is not None else _NullTelemetry()
        # The standalone 7A schema accepts zero; the live 7F loop applies the
        # Java runtime minimums (cost zero remains the disabled sentinel).
        self._budget_config = validate_runtime_budget_config(
            budget_config if budget_config is not None else AgentBudgetConfig()
        )
        self._execution_budget = (
            execution_budget
            if execution_budget is not None
            else AgentExecutionBudget(monotonic=monotonic, clock=clock)
        )
        self._usage_source = usage_source
        if execution_record_service is not None and not isinstance(
            execution_record_service, ExecutionRecordService
        ):
            # Keep replaceable test/application adapters possible while still
            # rejecting an accidental scalar configuration early.
            if not callable(getattr(execution_record_service, "start_or_resume", None)):
                raise TypeError("execution_record_service has no start_or_resume operation")
        self._execution_record_service = execution_record_service
        self._execution_record_context: ContextVar[
            DurableAgentExecutionRecord | None
        ] = ContextVar("dovideo_execution_record", default=None)

    @staticmethod
    def validate_context(context: VideoContext) -> VideoContext:
        """Apply Java ``validateContext`` before any adapter call."""

        if not isinstance(context, VideoContext):
            raise ValueError("Agent 需要目标和至少一个视频片段")
        if (
            not context.user_goal.strip()
            or not context.segments
            or any(segment is None for segment in context.segments)
        ):
            raise ValueError("Agent 需要目标和至少一个视频片段")
        return context

    async def select_relevant(
        self,
        context: VideoContext,
        media_id: int | None = None,
    ) -> VideoContext:
        """Validate and delegate bounded context selection exactly once."""

        context = self.validate_context(context)
        selected = await self._invoke(
            "LongVideoContext",
            self._context_service.select_relevant,
            context,
            media_id=media_id,
        )
        if not isinstance(selected, VideoContext):
            raise TypeError("select_relevant must return a VideoContext")
        await self._record_retrieval_selection(
            selected,
            purpose="initial",
            logical_event_id="retrieval.initial",
            agent_round=0,
        )
        return selected

    async def prepare_plan(
        self,
        context: VideoContext,
        media_id: int | None = None,
        saved_state: AgentState | None = None,
        profile: ModeProfile | None = None,
    ) -> tuple[VideoContext, AgentPlan]:
        """Select relevant context and then resolve its executable plan."""

        context = self.validate_context(context)
        relevant = await self.select_relevant(context, media_id=media_id)
        plan = await self.resolve_plan(media_id, relevant, saved_state, profile)
        return relevant, plan

    async def plan(
        self,
        context: VideoContext,
        media_id: int | None = None,
        saved_state: AgentState | None = None,
        profile: ModeProfile | None = None,
    ) -> AgentPlan:
        """Convenience operation returning only the resolved plan."""

        _relevant, plan = await self.prepare_plan(
            context, media_id=media_id, saved_state=saved_state, profile=profile
        )
        return plan

    async def resolve_plan(
        self,
        media_id: int | None,
        context: VideoContext,
        saved_state: AgentState | None = None,
        profile: ModeProfile | None = None,
    ) -> AgentPlan:
        """Resolve checkpoint > saved-state > Planner, repairing once at most."""

        context = self.validate_context(context)
        key = self._task_key(media_id, context, profile)
        plan: AgentPlan | Mapping[str, Any] | None = None

        if key is not None and self._checkpoint is not None:
            plan = await self._checkpoint.load_plan(key)
        if plan is None and saved_state is not None:
            plan = saved_state.plan

        should_persist = False
        repaired = False
        instruction = self._plan_instruction(profile)
        if plan is None:
            plan = await self._invoke(
                "Planner",
                self._planner.plan,
                context,
                _call_round=0,
                _call_reason="initial",
                instruction=instruction,
            )
            should_persist = True

        if not is_plan_valid(plan):
            # The Java path calls repairPlan once and validates its response;
            # an invalid repair is terminal for this slice, never re-repaired.
            plan = await self._invoke(
                "Planner repair",
                self._planner.repair_plan,
                context,
                plan,  # type: ignore[arg-type]
                _call_round=0,
                _call_reason="repair",
                instruction=instruction,
            )
            self._increment("planStructureRepairs")
            should_persist = True
            repaired = True

        executable_plan = validate_plan(plan)
        await self._record_plan(
            executable_plan,
            event_type=(
                ExecutionEventType.PLAN_REPAIRED
                if repaired
                else ExecutionEventType.PLAN_RECORDED
            ),
            logical_event_id="plan.repaired" if repaired else "plan.initial",
            repair_used=repaired,
            repair_attempts=1 if repaired else 0,
        )
        if key is not None and should_persist and self._checkpoint is not None:
            await self._checkpoint.save_plan(key, executable_plan)
        self._check_budget("Planner")
        await self._publish_stage(
            key,
            context.user_goal,
            profile,
            "Planner 已完成任务拆解",
            TaskStage.PLAN_COMPLETED,
        )
        return executable_plan

    async def execute_round(
        self,
        context: VideoContext,
        plan: AgentPlan,
        *,
        media_id: int | None = None,
        previous_critique: CriticResult | None = None,
        round: int = 1,
        profile: ModeProfile | None = None,
        _tool_budget: _ToolRequestBudget | None = None,
    ) -> AgentState:
        """Run one Executor call and checkpoint its Critic-less draft."""

        context = self.validate_context(context)
        executable_plan = validate_plan(plan)
        key = self._task_key(media_id, context, profile)
        await self._ensure_tool_calling_rollback_safe(key)
        await self._publish_stage(
            key,
            context.user_goal,
            profile,
            "Executor 正在按计划生成结构化产物",
            TaskStage.EXECUTOR_STARTED,
        )
        tool_round_outcome: _ToolRoundOutcome | None = None
        if self._executor_turn is None:
            # Keep the production/final-only ExecutorPort path byte-for-byte
            # compatible unless the independent X1-B seams are explicitly
            # injected by a caller.
            result = await self._invoke(
                "Executor",
                self._executor.execute,
                context,
                executable_plan,
                previous_critique,
                _call_round=round,
                _call_reason="rewrite" if previous_critique is not None else "initial",
                instruction=self._execute_instruction(profile),
            )
        else:
            tool_budget = (
                _tool_budget
                if _tool_budget is not None
                else _ToolRequestBudget(
                    self._tool_request_limit_per_round,
                    self._tool_request_limit_total,
                )
            )
            tool_round_outcome = await self._execute_tool_aware_round_outcome(
                context,
                executable_plan,
                previous_critique,
                media_id=media_id,
                round=round,
                profile=profile,
                tool_budget=tool_budget,
            )
            result = tool_round_outcome.result
        if not isinstance(result, AnalysisResult):
            raise TypeError("Executor must return an AnalysisResult")
        if self._executor_turn is None:
            await self._record_executor_turn(
                ExecutorTurn(
                    kind=ExecutorTurnKind.FINAL,
                    final_result=result,
                ),
                logical_event_id=f"executor.round.{round}.final",
                agent_round=round,
            )
        draft = AgentState(
            goal=context.user_goal,
            plan=executable_plan,
            result=result,
            critique=None,
            round=round,
        )
        if key is not None and self._checkpoint is not None:
            await self._checkpoint.save_execution_state(key, draft)
        # The draft is the higher-stage recovery authority.  Only after its
        # durable write succeeds may the last tool result be marked CONSUMED;
        # a failure of this final ledger write therefore cannot cause a tool
        # replay on the next attempt.
        if tool_round_outcome is not None and tool_round_outcome.finalize_consumption is not None:
            await tool_round_outcome.finalize_consumption()
        await self._publish_stage(
            key,
            context.user_goal,
            profile,
            "Executor 草稿已保存，开始校验证据",
            TaskStage.EXECUTOR_COMPLETED,
        )
        # The draft is durable before this check.  If usage crossed a limit,
        # the next run can resume this state directly at Critic.
        self._check_budget("Executor")
        return draft

    async def _execute_tool_aware_round_outcome(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None,
        *,
        media_id: int | None,
        round: int,
        profile: ModeProfile | None,
        tool_budget: _ToolRequestBudget,
    ) -> _ToolRoundOutcome:
        """Run the tool loop and optionally recover its durable ledger.

        The X1-B runtime-only path remains isolated in
        ``_execute_tool_aware_round_runtime``.  A caller must explicitly
        inject ``ToolCallCheckpointPort`` to opt into this X1-D1 branch.
        """

        if self._tool_checkpoint is None:
            return _ToolRoundOutcome(
                result=await self._execute_tool_aware_round_runtime(
                    context,
                    plan,
                    previous_critique,
                    media_id=media_id,
                    round=round,
                    profile=profile,
                    tool_budget=tool_budget,
                )
            )
        if self._executor_turn is None or self._tool_executor is None:
            raise TypeError("tool-aware Executor seams are incomplete")

        key = self._task_key(media_id, context, profile)
        if key is None:
            raise ToolRecoveryIdentityMismatchError(
                "durable tool execution requires a trusted TaskKey"
            )
        ledger = await self._load_tool_ledger(key)
        if ledger is None:
            ledger = empty_tool_state_ledger(key)
        self._validate_tool_ledger_identity(key, ledger)
        tool_budget.hydrate_from_ledger(ledger, round)

        exhausted_continuation_used = False
        pending_state = ledger.latest_for_round(round)
        if pending_state is not None:
            self._tool_metric("toolRecovered")
            self._validate_tool_state_identity(key, pending_state)
            exhausted_continuation_used = self._state_is_budget_denial(pending_state)
            if pending_state.execution_state is DurableToolExecutionState.REQUESTED:
                exhausted_continuation_used = (
                    exhausted_continuation_used
                    or self._has_prior_budget_denial(ledger, pending_state)
                )
            turn: ExecutorTurn | None = None
        else:
            turn = await self._invoke(
                "Executor",
                self._executor_turn.execute_turn,
                context,
                plan,
                previous_critique,
                instruction=self._execute_instruction(profile),
            )
            self._check_budget("Executor")

        last_state: DurableToolCallState | None = None
        last_result: ToolResult | None = None

        while True:
            if pending_state is None:
                if not isinstance(turn, ExecutorTurn):
                    raise TypeError("Executor turn must be an ExecutorTurn")
                if turn.kind is ExecutorTurnKind.FINAL:
                    if not isinstance(turn.final_result, AnalysisResult):
                        raise TypeError("FINAL Executor turn must contain AnalysisResult")
                    return _ToolRoundOutcome(result=turn.final_result)
                if turn.kind is not ExecutorTurnKind.TOOL_REQUEST:
                    raise TypeError("Executor turn has an unsupported kind")
                request = turn.tool_request
                if not isinstance(request, ModelToolRequest):
                    raise TypeError("TOOL_REQUEST turn must contain ModelToolRequest")

                previous_round_total = ledger.count_for_round(round)
                previous_total = ledger.request_count
                was_exhausted = (
                    previous_round_total >= tool_budget.per_round_limit
                    or previous_total >= tool_budget.total_limit
                )
                request_index = ledger.next_request_index
                pending_state = DurableToolCallState(
                    task_key=key,
                    agent_round=round,
                    request_index=request_index,
                    call_id=self._tool_call_id(request_index),
                    tool_name=request.tool_name,
                    request=request,
                    execution_state=DurableToolExecutionState.REQUESTED,
                )
                ledger = ledger.with_record(pending_state)
                # REQUESTED is the first durable fact for every model-issued
                # request.  A failure here leaves no recoverable call, which
                # is the explicitly permitted crash window A.
                await self._save_tool_ledger(key, ledger)
                self._tool_metric("toolRequests")
                tool_budget.hydrate_from_ledger(ledger, round)
                if (
                    was_exhausted
                    and exhausted_continuation_used
                    and pending_state.execution_state
                    is DurableToolExecutionState.REQUESTED
                ):
                    raise ToolTurnBudgetExceededError(
                        "Executor requested another tool after the bounded "
                        "budget-exhaustion continuation"
                    )
            else:
                request = pending_state.request
                if request is not None and request.tool_name != pending_state.tool_name:
                    raise ToolRecoveryIdentityMismatchError(
                        "recovered request/tool identity mismatch"
                    )
                previous_round_total, previous_total = ledger.counts_before(
                    pending_state.request_index
                )
                was_exhausted = (
                    previous_round_total >= tool_budget.per_round_limit
                    or previous_total >= tool_budget.total_limit
                )
                if (
                    was_exhausted
                    and exhausted_continuation_used
                    and pending_state.execution_state
                    is DurableToolExecutionState.REQUESTED
                ):
                    raise ToolTurnBudgetExceededError(
                        "Executor requested another tool after the bounded "
                        "budget-exhaustion continuation"
                    )

            state = pending_state
            if state is None:  # pragma: no cover - narrowed above
                raise ToolRecoveryIdentityMismatchError("missing durable tool state")

            # The ledger is the X1 result authority; this event is only the
            # historical Executor-turn reference.  Recording it on both the
            # first pass and recovery makes the logical append idempotent.
            if state.request is not None:
                await self._record_executor_turn_from_state(state)

            if state.execution_state is DurableToolExecutionState.REQUESTED:
                if request is None:
                    raise ToolRecoveryIdentityMismatchError(
                        "recovered REQUESTED state has no request snapshot"
                    )
                trusted_context = self._tool_policy_context(
                    context,
                    media_id=media_id,
                    profile=profile,
                    round=round,
                    tool_calls_this_round=previous_round_total,
                    tool_calls_total=previous_total,
                )
                if trusted_context is None:
                    decision = ToolPolicyDecision.deny(
                        ToolPolicyReasonCode.TASK_IDENTITY_MISMATCH
                    )
                else:
                    decision = self._tool_policy.evaluate(request, trusted_context)

                if decision.allowed and trusted_context is not None:
                    try:
                        tool_call = self._tool_policy.registry.create_call(
                            request,
                            call_id=state.call_id,
                        )
                    except Exception as error:
                        raise ToolRecoveryIdentityMismatchError(
                            "recovered tool arguments no longer match the registry"
                        ) from error
                    state = state.transition(
                        DurableToolExecutionState.AUTHORIZED,
                        validated_arguments=tool_call.validated_arguments,
                        canonical_args_digest=canonical_tool_arguments_digest(
                            tool_call.validated_arguments
                        ),
                        policy_decision=PolicyDecision.ALLOW,
                        policy_reason=None,
                    )
                else:
                    reason_code = decision.reason_code or ToolPolicyReasonCode.INVALID_ARGUMENTS
                    state = state.transition(
                        DurableToolExecutionState.DENIED,
                        policy_decision=PolicyDecision.DENY,
                        policy_reason=reason_code,
                    )
                ledger = ledger.with_record(state)
                await self._save_tool_ledger(key, ledger)
                self._tool_metric(
                    "toolAllowed" if decision.allowed else "toolDenied"
                )

            if state.execution_state is DurableToolExecutionState.DENIED:
                reason_code = state.policy_reason or ToolPolicyReasonCode.INVALID_ARGUMENTS
                tool_result = ToolResult(
                    call_id=state.call_id,
                    tool_name=state.tool_name,
                    status=ToolResultStatus.DENIED,
                    reason_code=reason_code,
                )
                state = state.transition(
                    DurableToolExecutionState.RESULT_STORED,
                    tool_result=tool_result,
                )
                ledger = ledger.with_record(state)
                await self._save_tool_ledger(key, ledger)
            elif state.execution_state in {
                DurableToolExecutionState.AUTHORIZED,
                DurableToolExecutionState.EXECUTING,
            }:
                tool_call = self._tool_call_from_durable_state(state)
                if state.execution_state is DurableToolExecutionState.AUTHORIZED:
                    state = state.transition(DurableToolExecutionState.EXECUTING)
                    ledger = ledger.with_record(state)
                    # The EXECUTING fact is durable before the read-only
                    # adapter is entered.  If the process dies afterwards,
                    # re-execution is explicitly at-least-once.
                    await self._save_tool_ledger(key, ledger)
                trusted_context = self._tool_policy_context(
                    context,
                    media_id=media_id,
                    profile=profile,
                    round=round,
                    tool_calls_this_round=previous_round_total,
                    tool_calls_total=previous_total,
                )
                if trusted_context is None:
                    raise ToolRecoveryIdentityMismatchError(
                        "trusted context could not be rebound for recovered tool"
                    )
                execution_context = ToolExecutionContext.from_policy_context(
                    trusted_context,
                    context,
                )
                # This counter is physical execution, not logical request
                # count: EXECUTING recovery may legitimately execute again.
                self._check_deadline("ToolExecutor")
                self._tool_metric("toolExecuted")
                tool_result = await self._invoke(
                    "ToolExecutor",
                    self._tool_executor.execute,
                    tool_call,
                    execution_context,
                    remaining_deadline=self._remaining_timeout_seconds(),
                )
                if not isinstance(tool_result, ToolResult):
                    raise TypeError("ToolExecutor must return a ToolResult")
                if (
                    tool_result.call_id != state.call_id
                    or tool_result.tool_name != state.tool_name
                ):
                    raise ValueError("ToolResult identity does not match ToolCall")
                state = state.transition(
                    DurableToolExecutionState.RESULT_STORED,
                    tool_result=tool_result,
                )
                ledger = ledger.with_record(state)
                # A typed FAILED/TRUNCATED result is just as complete as a
                # SUCCESS result.  Once this write succeeds, execution is
                # never repeated merely because the status is non-success.
                await self._save_tool_ledger(key, ledger)
                if tool_result.status is ToolResultStatus.FAILED:
                    self._tool_metric("toolFailures")
                if tool_result.status is ToolResultStatus.TRUNCATED or tool_result.truncated:
                    self._tool_metric("toolResultTruncated")
            elif state.execution_state in {
                DurableToolExecutionState.RESULT_STORED,
                DurableToolExecutionState.CONSUMED,
            }:
                if state.tool_result is None:
                    raise ToolRecoveryIdentityMismatchError(
                        "durable result state has no ToolResult"
                    )
                tool_result = state.tool_result
                self._tool_metric("toolResultReused")
            else:  # pragma: no cover - enum exhaustiveness guard
                raise ToolRecoveryIdentityMismatchError(
                    "unsupported durable tool execution state"
                )

            last_state = state
            last_result = tool_result
            await self._record_tool_reference(state, tool_result)
            self._check_budget("ToolExecutor")
            if was_exhausted:
                exhausted_continuation_used = True
                tools_available = False
            else:
                tools_available = tool_budget.has_capacity()

            turn = await self._invoke(
                "Executor continuation",
                self._executor_turn.continue_after_tool,
                context,
                plan,
                tool_result,
                previous_critique,
                instruction=self._execute_instruction(profile),
                tools_available=tools_available,
            )
            self._check_budget("Executor continuation")
            pending_state = None

            if not isinstance(turn, ExecutorTurn):
                raise TypeError("Executor turn must be an ExecutorTurn")
            if turn.kind is ExecutorTurnKind.FINAL:
                if not isinstance(turn.final_result, AnalysisResult):
                    raise TypeError("FINAL Executor turn must contain AnalysisResult")
                if last_state is None or last_result is None:
                    raise ToolRecoveryIdentityMismatchError(
                        "final tool turn has no durable result"
                    )
                await self._record_executor_turn(
                    turn,
                    logical_event_id=(
                        f"executor.round.{round}.final.after-tool."
                        f"{last_state.request_index}"
                    ),
                    agent_round=round,
                    request_index=last_state.request_index,
                )

                async def finalize_consumption(
                    *,
                    final_state=last_state,
                    final_ledger=ledger,
                    final_key=key,
                ) -> None:
                    consumed = final_state.transition(
                        DurableToolExecutionState.CONSUMED
                    )
                    await self._save_tool_ledger(
                        final_key,
                        final_ledger.with_record(consumed),
                    )

                return _ToolRoundOutcome(
                    result=turn.final_result,
                    finalize_consumption=finalize_consumption,
                )
            if turn.kind is not ExecutorTurnKind.TOOL_REQUEST:
                raise TypeError("Executor turn has an unsupported kind")
            request = turn.tool_request
            if not isinstance(request, ModelToolRequest):
                raise TypeError("TOOL_REQUEST turn must contain ModelToolRequest")

            # Persist the next logical request first.  If marking the previous
            # result CONSUMED fails afterwards, the latest durable request
            # still wins recovery and the previous tool cannot replay.
            next_state = DurableToolCallState(
                task_key=key,
                agent_round=round,
                request_index=ledger.next_request_index,
                call_id=self._tool_call_id(ledger.next_request_index),
                tool_name=request.tool_name,
                request=request,
                execution_state=DurableToolExecutionState.REQUESTED,
            )
            next_ledger = ledger.with_record(next_state)
            await self._save_tool_ledger(key, next_ledger)
            if last_state is None:
                raise ToolRecoveryIdentityMismatchError(
                    "next tool request has no previous durable result"
                )
            consumed = last_state.transition(DurableToolExecutionState.CONSUMED)
            ledger = next_ledger.with_record(consumed)
            await self._save_tool_ledger(key, ledger)
            tool_budget.hydrate_from_ledger(ledger, round)
            pending_state = next_state

    async def _execute_tool_aware_round(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None,
        *,
        media_id: int | None,
        round: int,
        profile: ModeProfile | None,
        tool_budget: _ToolRequestBudget,
    ) -> AnalysisResult:
        """Compatibility wrapper for callers of the old private seam."""

        outcome = await self._execute_tool_aware_round_outcome(
            context,
            plan,
            previous_critique,
            media_id=media_id,
            round=round,
            profile=profile,
            tool_budget=tool_budget,
        )
        if outcome.finalize_consumption is not None:
            await outcome.finalize_consumption()
        return outcome.result

    async def _load_tool_ledger(self, key: TaskKey) -> DurableToolStateLedger | None:
        if self._tool_checkpoint is None:
            return None
        loader = getattr(self._tool_checkpoint, "load_tool_state", None)
        if not callable(loader):
            raise TypeError("tool checkpoint has no load_tool_state operation")
        loaded = await self._invoke("Tool checkpoint load", loader, key)
        if loaded is None:
            return None
        if isinstance(loaded, DurableToolStateLedger):
            return loaded
        return DurableToolStateLedger.model_validate(loaded)

    async def _save_tool_ledger(
        self,
        key: TaskKey,
        ledger: DurableToolStateLedger,
    ) -> None:
        if self._tool_checkpoint is None:
            return
        self._validate_tool_ledger_identity(key, ledger)
        saver = getattr(self._tool_checkpoint, "save_tool_state", None)
        if not callable(saver):
            raise TypeError("tool checkpoint has no save_tool_state operation")
        await self._invoke("Tool checkpoint save", saver, key, ledger)

    @staticmethod
    def _tool_call_id(request_index: int) -> str:
        # The sequence is task-scoped, not a global/distributed counter.
        return f"tool-call-{request_index}"

    @staticmethod
    def _validate_tool_ledger_identity(
        key: TaskKey,
        ledger: DurableToolStateLedger,
    ) -> None:
        if ledger.task_key != key:
            raise ToolRecoveryIdentityMismatchError(
                "durable tool ledger does not belong to the current task"
            )
        for record in ledger.records:
            AgentLoopService._validate_tool_state_identity(key, record)

    @staticmethod
    def _validate_tool_state_identity(
        key: TaskKey,
        state: DurableToolCallState,
    ) -> None:
        if state.task_key != key:
            raise ToolRecoveryIdentityMismatchError(
                "durable tool call does not belong to the current task"
            )

    @staticmethod
    def _state_is_budget_denial(state: DurableToolCallState) -> bool:
        return bool(
            state.policy_reason is ToolPolicyReasonCode.BUDGET_EXHAUSTED
            or (
                state.tool_result is not None
                and state.tool_result.reason_code
                is ToolPolicyReasonCode.BUDGET_EXHAUSTED
            )
        )

    @staticmethod
    def _has_prior_budget_denial(
        ledger: DurableToolStateLedger,
        state: DurableToolCallState,
    ) -> bool:
        prior = [
            item
            for item in ledger.records
            if item.request_index < state.request_index
        ]
        return bool(prior and AgentLoopService._state_is_budget_denial(prior[-1]))

    def _tool_call_from_durable_state(
        self,
        state: DurableToolCallState,
    ) -> ToolCall:
        if state.validated_arguments is None or state.canonical_args_digest is None:
            raise ToolRecoveryIdentityMismatchError(
                "authorized durable tool state has no validated arguments"
            )
        try:
            tool_name = ToolName(state.tool_name)
            if self._tool_policy.registry.resolve(tool_name.value) is None:
                raise ValueError("tool is no longer registered")
            expected_digest = canonical_tool_arguments_digest(state.validated_arguments)
            if expected_digest != state.canonical_args_digest:
                raise ValueError("durable argument digest mismatch")
            return ToolCall(
                call_id=state.call_id,
                tool_name=tool_name,
                validated_arguments=state.validated_arguments,
            )
        except Exception as error:
            raise ToolRecoveryIdentityMismatchError(
                "durable tool call failed trusted registry revalidation"
            ) from error

    async def _execute_tool_aware_round_runtime(
        self,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None,
        *,
        media_id: int | None,
        round: int,
        profile: ModeProfile | None,
        tool_budget: _ToolRequestBudget,
    ) -> AnalysisResult:
        """Resolve bounded Executor tool turns before the Critic boundary.

        This is deliberately an application-only orchestration seam.  The
        existing ``ExecutorPort`` remains final-only, and no tool-aware turn
        is checkpointed or published as a separate Agent round.
        """

        if self._executor_turn is None or self._tool_executor is None:
            raise TypeError("tool-aware Executor seams are incomplete")

        turn = await self._invoke(
            "Executor",
            self._executor_turn.execute_turn,
            context,
            plan,
            previous_critique,
            instruction=self._execute_instruction(profile),
        )
        self._check_budget("Executor")
        exhausted_continuation_used = False

        while True:
            if not isinstance(turn, ExecutorTurn):
                raise TypeError("Executor turn must be an ExecutorTurn")
            if turn.kind is ExecutorTurnKind.FINAL:
                if not isinstance(turn.final_result, AnalysisResult):
                    raise TypeError("FINAL Executor turn must contain AnalysisResult")
                return turn.final_result
            if turn.kind is not ExecutorTurnKind.TOOL_REQUEST:
                raise TypeError("Executor turn has an unsupported kind")
            request = turn.tool_request
            if not isinstance(request, ModelToolRequest):
                raise TypeError("TOOL_REQUEST turn must contain ModelToolRequest")

            was_exhausted = not tool_budget.has_capacity()
            previous_round_total, previous_total, call_id = tool_budget.consume(round)
            if was_exhausted and exhausted_continuation_used:
                # Count this model-issued request before terminating the
                # bounded repair/continuation path; it must not loop forever.
                raise ToolTurnBudgetExceededError(
                    "Executor requested another tool after the bounded "
                    "budget-exhaustion continuation"
                )

            trusted_context = self._tool_policy_context(
                context,
                media_id=media_id,
                profile=profile,
                round=round,
                tool_calls_this_round=previous_round_total,
                tool_calls_total=previous_total,
            )
            if trusted_context is None:
                decision = ToolPolicyDecision.deny(
                    ToolPolicyReasonCode.TASK_IDENTITY_MISMATCH
                )
            else:
                decision = self._tool_policy.evaluate(request, trusted_context)

            if decision.allowed and trusted_context is not None:
                execution_context = ToolExecutionContext.from_policy_context(
                    trusted_context,
                    context,
                )
                tool_call = self._tool_policy.registry.create_call(
                    request,
                    call_id=call_id,
                )
                tool_result = await self._invoke(
                    "ToolExecutor",
                    self._tool_executor.execute,
                    tool_call,
                    execution_context,
                    remaining_deadline=self._remaining_timeout_seconds(),
                )
                if not isinstance(tool_result, ToolResult):
                    raise TypeError("ToolExecutor must return a ToolResult")
                if (
                    tool_result.call_id != call_id
                    or tool_result.tool_name != request.tool_name
                ):
                    raise ValueError("ToolResult identity does not match ToolCall")
            else:
                reason_code = decision.reason_code
                if reason_code is None:
                    reason_code = ToolPolicyReasonCode.INVALID_ARGUMENTS
                tool_result = ToolResult(
                    call_id=call_id,
                    tool_name=request.tool_name,
                    status=ToolResultStatus.DENIED,
                    reason_code=reason_code,
                )

            self._check_budget("ToolExecutor")
            if was_exhausted:
                # The first request observed after the cap is represented as
                # a typed BUDGET_EXHAUSTED denial and receives one final
                # continuation with tools disabled.
                exhausted_continuation_used = True
                tools_available = False
            else:
                tools_available = tool_budget.has_capacity()

            turn = await self._invoke(
                "Executor continuation",
                self._executor_turn.continue_after_tool,
                context,
                plan,
                tool_result,
                previous_critique,
                instruction=self._execute_instruction(profile),
                tools_available=tools_available,
            )
            self._check_budget("Executor continuation")

    def _tool_policy_context(
        self,
        context: VideoContext,
        *,
        media_id: int | None,
        profile: ModeProfile | None,
        round: int,
        tool_calls_this_round: int,
        tool_calls_total: int,
    ) -> ToolPolicyContext | None:
        """Build trusted policy identity exclusively from application state."""

        key = self._task_key(media_id, context, profile)
        if key is None:
            return None
        duration_ms = max(
            (segment.end_ms for segment in context.segments),
            default=0,
        )
        return ToolPolicyContext(
            task_key=key,
            media_id=key.media_id,
            mode=key.mode,
            agent_role=AgentRole.EXECUTOR,
            current_round=round,
            tool_calls_this_round=tool_calls_this_round,
            tool_calls_total=tool_calls_total,
            per_round_limit=self._tool_request_limit_per_round,
            total_limit=self._tool_request_limit_total,
            media_duration_ms=duration_ms,
        )

    async def critique_round(
        self,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult | None,
        *,
        media_id: int | None = None,
        round: int = 1,
        profile: ModeProfile | None = None,
    ) -> AgentState:
        """Run exactly one Critic boundary after an Executor draft.

        The order intentionally mirrors Java ``critiqueRound``: publish the
        start event, call/normalize Critic output, apply structure bounds,
        apply the existing Phase 6 evidence bounds, record metrics, construct
        state, checkpoint it, and finally publish the outcome stage.  Retry,
        evidence refresh, and loop control are deliberately left to later
        slices.
        """

        context = self.validate_context(context)
        if self._critic is None:
            raise TypeError("critic is required")
        key = self._task_key(media_id, context, profile)
        await self._publish_stage(
            key,
            context.user_goal,
            profile,
            "Critic 正在核验目标覆盖与时间戳证据",
            TaskStage.CRITIC_STARTED,
        )
        raw_critique = await self._invoke(
            "Critic",
            self._critic.critique,
            context,
            plan,
            result,
            _call_round=round,
            _call_reason="validation",
            instruction=self._critic_instruction(profile),
        )
        bound_result = _bind_evidence_provenance(context, result)
        normalized = normalize_critique(raw_critique)
        bounded = enforce_structure_bounds(bound_result, normalized, profile)
        critique = _enforce_evidence_bounds(context, bound_result, bounded)
        await self._record_critic(
            critique,
            logical_event_id=f"critic.round.{round}",
            agent_round=round,
        )
        # Legacy callers can invoke ``critique_round`` without X2-B recording
        # and may intentionally use pre-provenance test contexts.  Only the
        # enabled historical path requires the X2-A source revision.
        if (
            self._execution_record_service is not None
            and self._current_execution_id() is not None
        ):
            await self._record_evidence_verification(
                bound_result,
                critique,
                logical_event_id=f"evidence.round.{round}",
                agent_round=round,
                source_revision=_context_source_revision(context),
            )
        self._increment("criticRounds")
        if critique.passed:
            self._increment("criticPassed")

        state = AgentState(
            goal=context.user_goal,
            plan=plan,
            result=bound_result,
            critique=critique,
            round=round,
        )
        if key is not None:
            if self._checkpoint is not None:
                await self._checkpoint.save_critic_state(key, state)
            if critique.passed:
                message = "Critic 校验通过，正在整理结构化结果"
                stage = TaskStage.CRITIC_PASSED
            elif round >= self._budget_config.max_rounds:
                message = "Critic 达到最大校验轮次，正在保留警告并生成结果"
                stage = TaskStage.ANALYSIS_COMPLETED_WITH_WARNINGS
            elif self.requires_evidence_refresh(critique):
                message = "Critic 发现证据缺口，正在定向补充证据"
                stage = TaskStage.CRITIC_RETRY_REQUIRED
            else:
                message = "Critic 发现目标覆盖或结构问题，正在按反馈重写"
                stage = TaskStage.CRITIC_RETRY_REQUIRED
            await self._publish_stage(
                key, context.user_goal, profile, message, stage
            )
        return state

    @staticmethod
    def requires_evidence_refresh(critique: CriticResult | None) -> bool:
        """Return true only for timestamp/requirement/claim evidence gaps."""

        return bool(
            critique is not None
            and (
                bool(critique.required_timestamps)
                or bool(critique.missing_requirements)
                or bool(critique.unsupported_claims)
            )
        )

    requiresEvidenceRefresh = requires_evidence_refresh

    async def context_for_retry(
        self,
        media_id: int | None,
        full_context: VideoContext,
        selected_context: VideoContext,
        critique: CriticResult | None,
        profile: ModeProfile | None = None,
        *,
        record_round: int | None = None,
    ) -> VideoContext:
        """Refresh only evidence-driven retries; reuse context otherwise."""

        if not self.requires_evidence_refresh(critique):
            self._increment("criticRewriteOnlyRetries")
            return selected_context
        self._increment("criticEvidenceRefreshes")
        refined = await self._invoke(
            "LongVideoContext",
            self._context_service.refine_for_critique,
            media_id,
            full_context,
            selected_context,
            critique,
        )
        retry_round = 0 if record_round is None else max(0, int(record_round))
        await self._record_retrieval_selection(
            refined,
            purpose="critic_refinement",
            logical_event_id=f"retrieval.retry.{retry_round}",
            agent_round=retry_round,
        )
        await self._publish_stage(
            self._task_key(media_id, full_context, profile),
            full_context.user_goal,
            profile,
            "已按 Critic 反馈补充定向证据",
            TaskStage.EVIDENCE_REFRESHED,
        )
        return refined

    async def revise_plan_for_retry(
        self,
        media_id: int | None,
        context: VideoContext,
        current_plan: AgentPlan,
        critique: CriticResult | None,
        profile: ModeProfile | None = None,
        *,
        record_round: int | None = None,
    ) -> AgentPlan:
        """Replan only missing requirements and safely fall back on failure."""

        if critique is None or not critique.missing_requirements:
            return current_plan
        try:
            revised = await self._invoke(
                "Planner replan",
                self._planner.replan,
                context,
                current_plan,
                critique,
                _call_round=record_round,
                _call_reason="replan",
                instruction=self._plan_instruction(profile),
            )
            revised = validate_plan(revised)
            retry_round = 0 if record_round is None else max(0, int(record_round))
            await self._record_plan(
                revised,
                event_type=ExecutionEventType.PLAN_REPLANNED,
                logical_event_id=f"plan.replanned.{retry_round}",
                repair_used=False,
                repair_attempts=0,
            )
            self._increment("planRevisions")
            key = self._task_key(media_id, context, profile)
            if key is not None and self._checkpoint is not None:
                await self._checkpoint.save_plan(key, revised)
            self._check_budget("Planner")
            await self._publish_stage(
                key,
                context.user_goal,
                profile,
                "Planner 根据 Critic 反馈补充了遗漏任务",
                TaskStage.PLAN_COMPLETED,
            )
            return revised
        except (BudgetExceededError, DeadlineExceededError):
            # Budget/deadline failures are run-level termination signals, not
            # ordinary planner failures eligible for a silent fallback.
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self._find_deadline(error) is not None:
                raise
            self._increment("planRevisionFallbacks")
            return current_plan

    async def run(
        self,
        context: VideoContext,
        media_id: int | None = None,
        profile: ModeProfile | None = None,
    ) -> AgentState:
        """Run the bounded Planner–Executor–Critic loop under 7F budgets."""

        context = self.validate_context(context)
        termination_token = _BUDGET_TERMINATION_RECORDED.set(False)
        execution_token = None
        execution_record: DurableAgentExecutionRecord | None = None
        try:
            execution_record = await self._start_execution_record(
                context,
                media_id=media_id,
                profile=profile,
            )
            if execution_record is not None:
                if execution_record.status is not ExecutionRecordStatus.STARTED:
                    raise RuntimeError("current execution record is already terminal")
                execution_token = self._execution_record_context.set(execution_record)
            with self._execution_budget.open(self._budget_config.max_duration_ms):
                state = await self._run_with_budget(context, media_id, profile)
            if execution_record is not None:
                await self._execution_record_service.complete(
                    execution_record.execution_id,
                    state,
                )
            return state
        except BudgetExceededError:
            # Token/cost checks are already the canonical application error;
            # do not wrap or count them a second time.
            raise
        except DeadlineExceededError as error:
            self._record_budget_termination()
            raise BudgetExceededError(str(error)) from error
        except Exception as error:
            deadline = self._find_deadline(error)
            if deadline is None:
                # A provider-local TimeoutError (and every other ordinary
                # failure) remains unchanged and is not a budget termination.
                raise
            self._record_budget_termination()
            raise BudgetExceededError(str(deadline)) from error
        finally:
            if execution_token is not None:
                self._execution_record_context.reset(execution_token)
            _BUDGET_TERMINATION_RECORDED.reset(termination_token)

    async def _run_with_budget(
        self,
        context: VideoContext,
        media_id: int | None,
        profile: ModeProfile | None,
    ) -> AgentState:
        """Run one open budget scope; exceptions intentionally escape to run."""

        context = self.validate_context(context)
        max_rounds = self._budget_config.max_rounds
        key = self._task_key(media_id, context, profile)
        saved_state = (
            await self._checkpoint.load_critic_state(key)
            if key is not None and self._checkpoint is not None
            else None
        )

        terminal_checkpoint = bool(
            saved_state is not None
            and saved_state.result is not None
            and saved_state.critique is not None
            and (
                saved_state.round >= max_rounds
                or saved_state.critique.passed
            )
        )
        if terminal_checkpoint and is_plan_valid(saved_state.plan) and is_result_valid(
            saved_state.result, profile
        ):
            if key is not None and self._checkpoint is not None:
                await self._checkpoint.save_result(key, saved_state)
            self._increment("terminalCheckpointHits")
            return saved_state
        if terminal_checkpoint:
            self._increment("invalidTerminalCheckpointRepairs")
            saved_state = AgentState(
                goal=saved_state.goal,
                plan=saved_state.plan,
                result=saved_state.result,
                critique=saved_state.critique,
                round=0,
            )

        relevant = await self.select_relevant(context, media_id=media_id)
        plan = await self.resolve_plan(media_id, relevant, saved_state, profile)
        state = (
            saved_state
            if saved_state is not None
            else AgentState(goal=relevant.user_goal, plan=plan, round=0)
        )

        if state.critique is not None and not state.critique.passed:
            relevant = await self.context_for_retry(
                media_id,
                context,
                relevant,
                state.critique,
                profile,
                record_round=state.round,
            )
            plan = await self.revise_plan_for_retry(
                media_id,
                relevant,
                plan,
                state.critique,
                profile,
                record_round=state.round,
            )

        # A persisted Executor draft has already paid for generation.  Resume
        # directly at Critic and only execute another round if it fails.
        resumed_passed = False
        if self.can_resume_from_draft(state):
            self._increment("criticCheckpointResumes")
            self._check_budget("Executor Checkpoint")
            state = await self.critique_round(
                relevant,
                plan,
                state.result,
                media_id=media_id,
                round=state.round,
                profile=profile,
            )
            if state.critique is not None and state.critique.passed:
                resumed_passed = True
            elif state.round < max_rounds:
                relevant = await self.context_for_retry(
                    media_id,
                    context,
                    relevant,
                    state.critique,
                    profile,
                    record_round=state.round,
                )
                plan = await self.revise_plan_for_retry(
                    media_id,
                    relevant,
                    plan,
                    state.critique,
                    profile,
                    record_round=state.round,
                )

        if not resumed_passed:
            tool_budget = _ToolRequestBudget(
                self._tool_request_limit_per_round,
                self._tool_request_limit_total,
            )
            for current_round in range(state.round + 1, max_rounds + 1):
                self._check_budget(f"Agent Round {current_round}")
                state = await self.execute_round(
                    relevant,
                    plan,
                    media_id=media_id,
                    previous_critique=state.critique,
                    round=current_round,
                    profile=profile,
                    _tool_budget=tool_budget,
                )
                state = await self.critique_round(
                    relevant,
                    plan,
                    state.result,
                    media_id=media_id,
                    round=current_round,
                    profile=profile,
                )
                if state.critique is not None and state.critique.passed:
                    break
                if current_round < max_rounds:
                    relevant = await self.context_for_retry(
                        media_id,
                        context,
                        relevant,
                        state.critique,
                        profile,
                        record_round=current_round,
                    )
                    plan = await self.revise_plan_for_retry(
                        media_id,
                        relevant,
                        plan,
                        state.critique,
                        profile,
                        record_round=current_round,
                    )

        final_result = validate_result(state.result, profile)
        if key is not None and self._checkpoint is not None:
            await self._checkpoint.save_result(key, state)
        # ``final_result`` is intentionally only a validation result here;
        # preserving the state object keeps Critic warnings and round data.
        del final_result
        return state

    contextForRetry = context_for_retry
    revisePlanForRetry = revise_plan_for_retry

    async def run_once(
        self,
        context: VideoContext,
        media_id: int | None = None,
        saved_state: AgentState | None = None,
        profile: ModeProfile | None = None,
        *,
        previous_critique: CriticResult | None = None,
        round: int = 1,
    ) -> AgentState:
        """Select, resolve, and execute one round without invoking Critic."""

        relevant, plan = await self.prepare_plan(
            context, media_id=media_id, saved_state=saved_state, profile=profile
        )
        tool_budget = _ToolRequestBudget(
            self._tool_request_limit_per_round,
            self._tool_request_limit_total,
        )
        return await self.execute_round(
            relevant,
            plan,
            media_id=media_id,
            previous_critique=previous_critique,
            round=round,
            profile=profile,
            _tool_budget=tool_budget,
        )

    async def _invoke(
        self,
        stage: str,
        operation: Any,
        *args: Any,
        _call_round: int | None = None,
        _call_reason: str = "normal",
        **kwargs: Any,
    ) -> Any:
        """Invoke one async provider call with scoped usage metadata."""

        scope = getattr(self._telemetry, "model_call_scope", None)
        manager = (
            scope(round=_call_round, reason=_call_reason)
            if callable(scope)
            else nullcontext()
        )
        with manager:
            return await self._invoke_unscoped(stage, operation, *args, **kwargs)

    async def _invoke_unscoped(
        self,
        stage: str,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Apply the active deadline timeout to one injected operation."""

        self._check_deadline(stage)
        timeout = self._remaining_timeout_seconds()
        result = operation(*args, **kwargs)
        if not inspect.isawaitable(result):
            return result
        if timeout is None:
            # A provider's own TimeoutError is an ordinary adapter failure
            # when no Agent wait timeout fired.
            return await result

        # ``asyncio.wait_for`` raises the same TimeoutError type when the
        # awaitable itself raises one, making those two cases indistinguish-
        # able.  Wait on a child task explicitly so only a still-pending task
        # at the timeout boundary becomes a budget deadline.
        task = asyncio.ensure_future(result)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise DeadlineExceededError(
                f"{stage} 超时：Agent 已耗尽执行时长预算"
            )
        # Let provider exceptions (including TimeoutError) escape unchanged.
        return task.result()

    @staticmethod
    def _find_deadline(error: BaseException) -> DeadlineExceededError | None:
        """Find a deadline through a bounded cause/context chain."""

        current: BaseException | None = error
        seen: set[int] = set()
        for _ in range(16):
            if current is None or id(current) in seen:
                return None
            seen.add(id(current))
            if isinstance(current, DeadlineExceededError):
                return current
            # Explicit ``raise ... from ...`` takes precedence; implicit
            # context is the fallback used by ordinary exception wrapping.
            current = current.__cause__ or current.__context__
        return None

    def _check_deadline(self, stage: str) -> None:
        checker = getattr(self._execution_budget, "check", None)
        if callable(checker):
            checker(stage)
            return
        remaining = getattr(self._execution_budget, "remaining_seconds", None)
        if callable(remaining) and remaining() is not None:
            return

    def _remaining_timeout_seconds(self) -> float | None:
        remaining = getattr(self._execution_budget, "remaining_seconds", None)
        if callable(remaining):
            return remaining()
        remaining_ms = getattr(self._execution_budget, "remaining_ms", None)
        if callable(remaining_ms):
            value = remaining_ms()
            return None if value is None else float(value) / 1000.0
        return None

    def _current_usage(self) -> BudgetUsage:
        source = self._usage_source
        if source is None:
            telemetry_current = getattr(self._telemetry, "current_usage", None)
            if not callable(telemetry_current):
                telemetry_current = getattr(self._telemetry, "currentUsage", None)
            if callable(telemetry_current):
                source = self._telemetry
        if source is None:
            return BudgetUsage()
        if callable(source):
            raw_usage = source()
        else:
            getter = getattr(source, "current_usage", None)
            if not callable(getter):
                getter = getattr(source, "current", None)
            if not callable(getter):
                getter = getattr(source, "currentUsage", None)
            if not callable(getter):
                raise InvalidBudgetError(
                    "Agent 预算用量源必须提供 current_usage()"
                )
            raw_usage = getter()
        return validate_budget_usage(raw_usage)

    def _check_budget(self, stage: str) -> None:
        """Check deadline and provider-reported usage using strict ``>`` caps."""

        self._check_deadline(stage)
        usage = self._current_usage()
        if usage.estimated_tokens > self._budget_config.max_estimated_tokens:
            self._raise_budget(
                f"{stage} 后终止：Agent 超过最大 Token 预算 "
                f"{self._budget_config.max_estimated_tokens}"
            )
        if (
            self._budget_config.max_estimated_cost > 0
            and usage.estimated_cost > self._budget_config.max_estimated_cost
        ):
            self._raise_budget(
                f"{stage} 后终止：Agent 超过最大成本预算 "
                f"{self._budget_config.max_estimated_cost}"
            )

    def _raise_budget(self, message: str) -> None:
        self._record_budget_termination()
        raise BudgetExceededError(message)

    def _record_budget_termination(self) -> None:
        if _BUDGET_TERMINATION_RECORDED.get():
            return
        self._increment("budgetTerminations")
        _BUDGET_TERMINATION_RECORDED.set(True)

    def _increment(self, metric: str, amount: int = 1) -> None:
        increment = getattr(self._telemetry, "increment", None)
        if increment is not None:
            increment(metric, amount)

    async def _start_execution_record(
        self,
        context: VideoContext,
        *,
        media_id: int | None,
        profile: ModeProfile | None,
    ) -> DurableAgentExecutionRecord | None:
        service = self._execution_record_service
        if service is None:
            return None
        key = self._task_key(media_id, context, profile)
        if key is None:
            raise RuntimeError("durable execution recording requires a trusted TaskKey")
        load_execution = getattr(service, "load_for_task", None)
        existing = await load_execution(key) if callable(load_execution) else None
        if (
            callable(load_execution)
            and existing is None
            and await self._legacy_execution_checkpoint_exists(key)
        ):
            # A checkpoint predating X2-B is recovery state, not a complete
            # historical record.  Do not promote it into a replayable record
            # or claim that the earlier Planner/Executor/Critic turns exist.
            raise ExecutionRecordConflictError(
                "legacy checkpoint has no durable X2-B execution history"
            )
        return await service.start_or_resume(
            key,
            media_identity=context.source,
            source_revision=_context_source_revision(context),
            source_provenance_version=(
                context.provenance_version or "x2-a-v1"
            ),
        )

    async def _legacy_execution_checkpoint_exists(self, key: TaskKey) -> bool:
        checkpoint = self._checkpoint
        if checkpoint is None:
            return False
        for operation in ("load_plan", "load_critic_state"):
            loader = getattr(checkpoint, operation, None)
            if not callable(loader):
                continue
            if await self._invoke("Execution checkpoint legacy check", loader, key) is not None:
                return True
        return False

    def _current_execution_id(self) -> str | None:
        record = self._execution_record_context.get()
        return None if record is None else record.execution_id

    async def _record_plan(
        self,
        plan: AgentPlan,
        *,
        event_type: ExecutionEventType,
        logical_event_id: str,
        repair_used: bool,
        repair_attempts: int,
    ) -> None:
        service = self._execution_record_service
        execution_id = self._current_execution_id()
        if service is None or execution_id is None:
            return
        await service.record_plan(
            execution_id,
            plan,
            event_type=event_type,
            logical_event_id=logical_event_id,
            repair_used=repair_used,
            repair_attempts=repair_attempts,
        )

    async def _record_retrieval_selection(
        self,
        selected: VideoContext,
        *,
        purpose: str,
        logical_event_id: str,
        agent_round: int,
    ) -> None:
        service = self._execution_record_service
        execution_id = self._current_execution_id()
        if service is None or execution_id is None:
            return
        await service.record_retrieval_selection(
            execution_id,
            selected.segments,
            purpose=purpose,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            budget_truncated=False,
        )

    async def _record_executor_turn(
        self,
        turn: ExecutorTurn,
        *,
        logical_event_id: str,
        agent_round: int,
        request_index: int | None = None,
        args_digest: str | None = None,
    ) -> None:
        service = self._execution_record_service
        execution_id = self._current_execution_id()
        if service is None or execution_id is None:
            return
        await service.record_executor_turn(
            execution_id,
            turn,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            request_index=request_index,
            args_digest=args_digest,
        )

    async def _record_executor_turn_from_state(
        self,
        state: DurableToolCallState,
    ) -> None:
        if state.request is None:
            return
        await self._record_executor_turn(
            ExecutorTurn(
                kind=ExecutorTurnKind.TOOL_REQUEST,
                tool_request=state.request,
            ),
            logical_event_id=(
                f"executor.round.{state.agent_round}.tool.{state.request_index}"
            ),
            agent_round=state.agent_round,
            request_index=state.request_index,
            args_digest=state.canonical_args_digest,
        )

    async def _record_tool_reference(
        self,
        state: DurableToolCallState,
        tool_result: ToolResult,
    ) -> None:
        service = self._execution_record_service
        execution_id = self._current_execution_id()
        if service is None or execution_id is None:
            return
        policy = None if state.policy_decision is None else state.policy_decision.value
        reason = None if state.policy_reason is None else state.policy_reason.value
        args_digest = state.canonical_args_digest or ""
        if not args_digest and state.validated_arguments is not None:
            args_digest = canonical_tool_arguments_digest(
                state.validated_arguments
            )
        if not args_digest:
            args_digest = "unvalidated"
        await service.record_tool_reference(
            execution_id,
            logical_event_id=f"tool.{state.call_id}.result",
            agent_round=state.agent_round,
            call_id=state.call_id,
            request_index=state.request_index,
            tool_name=state.tool_name,
            args_digest=args_digest,
            policy_decision=policy,
            reason_code=reason,
            result_status=tool_result.status.value,
            ledger_reference=f"tool-call:{state.call_id}",
        )

    async def _record_critic(
        self,
        critique: CriticResult,
        *,
        logical_event_id: str,
        agent_round: int,
    ) -> None:
        service = self._execution_record_service
        execution_id = self._current_execution_id()
        if service is None or execution_id is None:
            return
        await service.record_critic(
            execution_id,
            critique,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
        )

    async def _record_evidence_verification(
        self,
        result: AnalysisResult | None,
        critique: CriticResult,
        *,
        logical_event_id: str,
        agent_round: int,
        source_revision: str,
    ) -> None:
        service = self._execution_record_service
        execution_id = self._current_execution_id()
        if service is None or execution_id is None:
            return
        await service.record_evidence_verification(
            execution_id,
            result,
            critique,
            logical_event_id=logical_event_id,
            agent_round=agent_round,
            source_revision=source_revision,
        )

    def _tool_metric(self, metric: str, amount: int = 1) -> None:
        """Record bounded X1 counters without changing task correctness.

        Checkpoint writes remain correctness dependencies.  These counters are
        operational diagnostics only, so a Redis/trace failure must never
        turn a durable tool result into a retry or change the policy path.
        """

        try:
            increment = getattr(self._telemetry, "increment", None)
            if callable(increment):
                increment(metric, amount)
        except Exception:
            return

    async def _ensure_tool_calling_rollback_safe(
        self,
        key: TaskKey | None,
    ) -> None:
        """Do not silently abandon an in-flight ledger after flag rollback."""

        if (
            self._tool_calling_enabled is not False
            or self._tool_checkpoint is None
            or key is None
        ):
            return
        ledger = await self._load_tool_ledger(key)
        if ledger is None:
            return
        self._validate_tool_ledger_identity(key, ledger)
        if any(
            record.execution_state is not DurableToolExecutionState.CONSUMED
            for record in ledger.records
        ):
            raise ToolCallingDisabledWithInFlightStateError(
                "X1 tool calling is disabled while durable tool state is in flight"
            )

    @staticmethod
    def can_resume_from_draft(state: AgentState | None) -> bool:
        """Recognize the Java draft-checkpoint handoff to the later Critic.

        This predicate intentionally performs no Critic call.  It only makes
        the recovery condition explicit so 7D+ can continue from a saved
        Executor draft without generating it again.
        """

        return bool(
            isinstance(state, AgentState)
            and state.result is not None
            and state.critique is None
            and state.round > 0
        )

    canResumeFromDraft = can_resume_from_draft

    async def _publish_stage(
        self,
        key: TaskKey | None,
        goal: str,
        profile: ModeProfile | None,
        message: str,
        stage: TaskStage,
    ) -> None:
        if key is None or self._event_publisher is None:
            return
        status = TaskStatus.of(TaskStatusState.PROCESSING, message)
        event = TaskEvent.of(status, stage)
        await self._event_publisher.publish(key, event)

    @staticmethod
    def _task_key(
        media_id: int | None,
        context: VideoContext,
        profile: ModeProfile | None,
    ) -> TaskKey | None:
        if media_id is None:
            return None
        return TaskKey(media_id, context.user_goal, AgentLoopService._mode_of(profile))

    @staticmethod
    def _mode_of(profile: ModeProfile | None) -> AnalysisMode:
        if profile is None or profile.mode is None:
            return AnalysisMode.GENERAL
        return profile.mode

    @staticmethod
    def _plan_instruction(profile: ModeProfile | None) -> str:
        return "" if profile is None else profile.plan_instruction

    @staticmethod
    def _execute_instruction(profile: ModeProfile | None) -> str:
        return "" if profile is None else profile.execute_instruction

    @staticmethod
    def _critic_instruction(profile: ModeProfile | None) -> str:
        return "" if profile is None else profile.critic_instruction

    # Java migration spellings.  The aliases that need argument-order
    # adaptation are wrappers below rather than raw unbound aliases.
    validateContext = validate_context
    selectRelevant = select_relevant
    preparePlan = prepare_plan
    runOnce = run_once

    async def resolvePlan(
        self,
        media_id: int | None,
        context: VideoContext,
        saved_state: AgentState | None = None,
        profile: ModeProfile | None = None,
    ) -> AgentPlan:
        return await self.resolve_plan(media_id, context, saved_state, profile)

    async def executeRound(
        self,
        media_id: int | None,
        context: VideoContext,
        plan: AgentPlan,
        previous_critique: CriticResult | None = None,
        round: int = 1,
        profile: ModeProfile | None = None,
    ) -> AgentState:
        return await self.execute_round(
            context,
            plan,
            media_id=media_id,
            previous_critique=previous_critique,
            round=round,
            profile=profile,
        )

    async def critiqueRound(
        self,
        media_id: int | None,
        context: VideoContext,
        plan: AgentPlan,
        result: AnalysisResult | None,
        round: int = 1,
        profile: ModeProfile | None = None,
    ) -> AgentState:
        return await self.critique_round(
            context,
            plan,
            result,
            media_id=media_id,
            round=round,
            profile=profile,
        )


def _context_source_revision(context: VideoContext) -> str:
    """Read the X2-A revision without manufacturing a new provenance value."""

    if context.source_revision.strip():
        return context.source_revision.strip()
    for segment in context.segments:
        if segment.source_revision.strip():
            return segment.source_revision.strip()
    raise RuntimeError("durable execution recording requires source_revision")


__all__ = [
    "AgentLoopService",
    "ToolCallingDisabledWithInFlightStateError",
    "ToolTurnBudgetExceededError",
    *_POLICY_EXPORTS,
]

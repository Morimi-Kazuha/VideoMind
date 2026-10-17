"""Extracted production telemetry boundary; legacy exports remain in r4_runtime."""
from __future__ import annotations
import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dovideo.application.follow_up_budget import estimate_chat_input
from typing import Any, Mapping, Sequence
from dovideo.application.value_objects import (TaskKey)
from dovideo.application.errors import (BudgetExceededError)
from dovideo.domain import BudgetUsage, TaskEvent, AgentBudgetConfig
from dovideo.infrastructure.media import SubprocessExecutionError, SubprocessLaunchError, SubprocessTimeoutError
from dovideo.infrastructure.redis_observability import RedisAgentTelemetry

class R4AgentTelemetry:
    """Use the existing Redis trace as both metrics and budget usage sink."""

    def __init__(
        self,
        store: RedisAgentTelemetry,
        *,
        budget_config: AgentBudgetConfig | None = None,
    ) -> None:
        self.store = store
        self._budget_config = budget_config
        self._model_identifier = ""
        self._current_key: ContextVar[TaskKey | None] = ContextVar(
            "dovideo_r4_telemetry_task",
            default=None,
        )
        self._scoped_counters: ContextVar[dict[str, int] | None] = ContextVar(
            "dovideo_r4_scoped_counters",
            default=None,
        )
        self._chat_usage_capture: ContextVar[list[dict[str, Any]] | None] = ContextVar(
            "dovideo_r4_chat_usage_capture", default=None
        )
        self._model_call_context: ContextVar[tuple[int | None, str]] = ContextVar(
            "dovideo_r4_model_call_context", default=(None, "normal")
        )

    @contextmanager
    def model_call_scope(self, *, round: int | None, reason: str) -> Iterator[None]:
        token = self._model_call_context.set((round, str(reason)[:32]))
        try:
            yield
        finally:
            self._model_call_context.reset(token)

    def admit_model_call(
        self,
        *,
        stage: str,
        model: str,
        messages: Sequence[Mapping[str, str]],
        attempt: int,
        max_output_tokens: int | None,
    ) -> dict[str, Any] | None:
        """Reserve measured input and stage output headroom before HTTP work.

        The input estimate is deliberately conservative; provider usage replaces
        it for completed calls. No prompt or media text enters the trace.
        """

        key = self._current_key.get()
        config = self._budget_config
        if key is None or config is None or self._scoped_counters.get() is not None:
            return None
        input_estimate = estimate_chat_input(messages)
        reserves = {
            "CHUNK_SUMMARY": 4096,
            "RETRIEVAL_PLANNER": 1024,
            "ADAPTIVE_RETRIEVAL_PLANNER": 1024,
            "PLANNER": 2048,
            "PLANNER_REPAIR": 2048,
            "REPLANNER": 2048,
            "EXECUTOR": 8192,
            "EXECUTOR_REPAIR": 8192,
            "EXECUTOR_TURN": 8192,
            "EXECUTOR_CONTINUATION": 8192,
            "CRITIC": 16000,
        }
        stage_reserve = reserves.get(stage.upper(), 8192)
        output_reserve = (
            min(max_output_tokens, stage_reserve)
            if max_output_tokens is not None
            else stage_reserve
        )
        usage = self.store.current_usage_for_key(key)
        remaining = config.max_estimated_tokens - usage.estimated_tokens
        allowed = (
            (not config.token_budget_enabled or input_estimate + output_reserve <= remaining)
            and (
                config.max_estimated_cost <= 0
                or usage.estimated_cost < config.max_estimated_cost
            )
        )
        call_round, call_reason = self._model_call_context.get()
        admission = {
            "stage": str(stage)[:48],
            "model": str(model)[:128],
            "attempt": max(1, int(attempt)),
            "round": call_round,
            "reason": "repair" if stage.upper().endswith("_REPAIR") else call_reason,
            "inputEstimate": input_estimate,
            "outputReserve": output_reserve,
            "cumulativeBefore": usage.estimated_tokens,
            "remainingBefore": max(0, remaining) if config.token_budget_enabled else None,
            "tokenBudgetEnabled": config.token_budget_enabled,
            "allowed": allowed,
        }
        self.store.record_structural_for_key(
            key, {"kind": "modelCallAdmission", **admission}
        )
        if not allowed:
            self.increment("modelBudgetDenials")
            self.increment("budgetTerminations")
            raise BudgetExceededError(
                "本次分析超过执行预算，请缩小分析范围或重试。"
            )
        return admission

    def record_unreported_model_usage(
        self,
        *,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        provider_reported_cost: float | None = None,
    ) -> None:
        """Charge a conservative fallback when a sent request has no usage."""

        self.record_chat_usage(
            stage=stage,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            provider_reported_cost=provider_reported_cost,
            usage_source=(
                "heuristic_tokens_provider_cost"
                if provider_reported_cost is not None
                else "heuristic"
            ),
        )
        self.add(
            estimated_tokens=input_tokens + output_tokens,
            estimated_cost=0.0 if provider_reported_cost is None else provider_reported_cost,
        )

    def finish_model_call(
        self,
        admission: Mapping[str, Any] | None,
        *,
        input_tokens: int | float | None,
        output_tokens: int | float | None,
        total_tokens: int | float | None,
        usage_source: str,
    ) -> None:
        key = self._current_key.get()
        if key is None or admission is None or self._scoped_counters.get() is not None:
            return
        usage = self.store.current_usage_for_key(key)
        limit = self._budget_config.max_estimated_tokens if self._budget_config else 0
        self.store.record_structural_for_key(
            key,
            {
                "kind": "modelCallUsage",
                "stage": admission["stage"],
                "attempt": admission["attempt"],
                "round": admission["round"],
                "reason": admission["reason"],
                "inputEstimate": admission["inputEstimate"],
                "outputReserve": admission["outputReserve"],
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": total_tokens,
                "usageSource": usage_source,
                "cumulativeTotal": usage.estimated_tokens,
                "remainingBudget": (max(0, limit - usage.estimated_tokens)
                    if self._budget_config and self._budget_config.token_budget_enabled else None),
                "tokenBudgetEnabled": self._budget_config.token_budget_enabled if self._budget_config else True,
            },
        )
        if self._budget_config and (
            (self._budget_config.token_budget_enabled
             and usage.estimated_tokens > self._budget_config.max_estimated_tokens)
            or (
                self._budget_config.max_estimated_cost > 0
                and usage.estimated_cost > self._budget_config.max_estimated_cost
            )
        ):
            self.increment("modelBudgetOverruns")
            self.increment("budgetTerminations")
            raise BudgetExceededError(
                "本次分析超过执行预算，请缩小分析范围或重试。"
            )

    def set_model_identifier(self, model: str | None) -> None:
        value = str(model or "").strip()
        self._model_identifier = value[:128]

    def start(self, key: TaskKey) -> str:
        return self.store.start(key)

    def bind(self, key: TaskKey):
        if not self.store.latest(key):
            self.store.start(key)
        return self._current_key.set(key)

    def reset(self, token: Any) -> None:
        self._current_key.reset(token)

    @contextmanager
    def capture_chat_usage(self) -> Iterator[list[dict[str, Any]]]:
        """Capture exact provider usage for one caller-scoped execution."""

        records: list[dict[str, Any]] = []
        token = self._chat_usage_capture.set(records)
        try:
            yield records
        finally:
            self._chat_usage_capture.reset(token)

    def record_chat_usage(
        self,
        *,
        stage: str,
        model: str,
        input_tokens: int | float | None,
        output_tokens: int | float | None,
        total_tokens: int | float | None,
        provider_reported_cost: int | float | None,
        usage_source: str = "provider",
    ) -> None:
        """Retain provider counts without estimating missing fields."""

        record = {
            "stage": _role_name(stage),
            "model": str(model)[:128],
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
            "providerReportedCost": provider_reported_cost,
            "usageSource": usage_source,
        }
        capture = self._chat_usage_capture.get()
        if capture is not None:
            capture.append(record)
        key = self._current_key.get()
        if key is not None and self._scoped_counters.get() is None:
            self.store.record_structural_for_key(key, {"kind": "chatUsage", **record})

    @contextmanager
    def isolated_metrics(self) -> Iterator[dict[str, int]]:
        """Capture retrieval counters locally without touching task traces.

        Follow-up is not a canonical task execution.  The strict R4 retrieval
        adapter still needs to observe its fallback counters, so keep those
        counters request-local and discard them when the interaction ends.
        ContextVar keeps concurrent API requests isolated.
        """

        counters: dict[str, int] = {}
        token = self._scoped_counters.set(counters)
        try:
            yield counters
        finally:
            self._scoped_counters.reset(token)

    def record(self, key: TaskKey, event: TaskEvent) -> None:
        if self._scoped_counters.get() is not None:
            return
        self.store.record(key, event)

    def latest(self, key: TaskKey) -> dict[str, Any]:
        return self.store.latest(key)

    def project_execution(
        self,
        key: TaskKey,
        *,
        execution_id: str,
        status: str,
        event_type: str | None,
        latest_sequence: int,
        recorded_semantic_events: int,
    ) -> None:
        """Project a durable execution fact into the existing Redis trace."""

        projector = getattr(self.store, "record_execution_projection", None)
        if callable(projector):
            projector(
                key,
                execution_id=execution_id,
                status=status,
                event_type=event_type,
                latest_sequence=latest_sequence,
                recorded_semantic_events=recorded_semantic_events,
            )
            return
        # Test/legacy trace adapters without the additive helper still get a
        # bounded structural diagnostic; they never receive event payloads.
        self.store.record_structural_for_key(
            key,
            {
                "kind": "executionRecord",
                "executionId": str(execution_id)[:96],
                "executionRecordStatus": str(status)[:32],
                "eventType": None if event_type is None else str(event_type)[:64],
                "latestRecordedSequence": max(0, int(latest_sequence)),
                "recordedSemanticEvents": max(0, int(recorded_semantic_events)),
            },
        )

    def _key(self, trace: Any | None = None) -> TaskKey | None:
        candidate = getattr(trace, "task_key", None) if trace is not None else None
        return candidate if isinstance(candidate, TaskKey) else self._current_key.get()

    def increment(
        self,
        metric: str,
        amount: int = 1,
        *,
        trace: Any | None = None,
    ) -> None:
        scoped = self._scoped_counters.get()
        if scoped is not None:
            scoped[metric] = scoped.get(metric, 0) + amount
            return
        key = self._key(trace)
        if key is not None:
            self.store.increment_for_key(key, metric, amount)

    def observe(
        self,
        metric: str,
        value: float,
        *,
        trace: Any | None = None,
    ) -> None:
        if self._scoped_counters.get() is not None:
            return
        key = self._key(trace)
        if key is not None:
            self.store.observe_for_key(key, metric, value)

    def add(
        self,
        estimated_tokens: int | float = 0,
        estimated_cost: float = 0.0,
        *,
        usage: BudgetUsage | Mapping[str, Any] | None = None,
    ) -> BudgetUsage:
        key = self._current_key.get()
        if usage is not None:
            if estimated_tokens != 0 or estimated_cost != 0.0:
                raise TypeError("usage cannot be combined with delta values")
            validated = BudgetUsage.model_validate(usage)
            estimated_tokens = validated.estimated_tokens
            estimated_cost = validated.estimated_cost
        if key is None or self._scoped_counters.get() is not None:
            return BudgetUsage(
                estimatedTokens=max(0, int(float(estimated_tokens))),
                estimatedCost=max(0.0, float(estimated_cost)),
            )
        return self.store.add_usage_for_key(
            key,
            estimated_tokens=estimated_tokens,
            estimated_cost=estimated_cost,
        )

    def current_usage(self) -> BudgetUsage:
        key = self._current_key.get()
        return BudgetUsage() if key is None else self.store.current_usage_for_key(key)

    currentUsage = current_usage

    def counter_value(self, metric: str) -> int:
        scoped = self._scoped_counters.get()
        if scoped is not None:
            return int(scoped.get(metric, 0))
        key = self._current_key.get()
        if key is None:
            return 0
        document = self.store.latest(key)
        counters = document.get("counters", {})
        return int(counters.get(metric, 0)) if isinstance(counters, dict) else 0

    def record_model_transport(
        self,
        stage: str,
        *,
        status_code: int,
        finish_reason: str | None,
        content_present: bool,
        content_chars: int,
    ) -> None:
        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "transport",
                "stage": _role_name(stage),
                "model": self._model_identifier,
                "statusCode": int(status_code),
                "finishReason": None if finish_reason is None else str(finish_reason)[:64],
                "contentPresent": bool(content_present),
                "contentChars": max(0, int(content_chars)),
            },
        )

    def record_structured_response(
        self,
        stage: str,
        diagnostic: Mapping[str, Any],
    ) -> None:
        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None or not isinstance(diagnostic, Mapping):
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "structured",
                "stage": _role_name(stage),
                "model": self._model_identifier,
                "diagnostic": dict(diagnostic),
            },
        )

    def record_model_routing(
        self,
        *,
        enabled: bool,
        lane: str,
        fallback: bool,
        reason: str,
        confidence: float,
    ) -> None:
        """Record additive bounded routing diagnostics in the existing trace."""

        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "modelRouting",
                "modelRoutingEnabled": bool(enabled),
                "modelRouteLane": str(lane)[:16],
                "modelRouteFallback": bool(fallback),
                "modelRouteReason": str(reason)[:48],
                "modelRouteConfidence": max(0.0, min(1.0, float(confidence))),
            },
        )

    def record_jev_routing(
        self,
        *,
        status_code: int | None,
        latency_ms: float,
        gateway: str,
        decision_model: str,
        router_model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        usage_cost_usd: float | None,
        fallback: bool,
    ) -> None:
        """Persist only bounded Jev transport metadata; never raw payloads."""

        capture = self._chat_usage_capture.get()
        if capture is not None:
            capture.append({
                "stage": "ROUTER",
                "model": str(decision_model)[:128],
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "totalTokens": (
                    input_tokens + output_tokens
                    if input_tokens is not None and output_tokens is not None else None
                ),
                "providerReportedCost": usage_cost_usd,
            })
        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        safe_gateway = (
            gateway
            if gateway in {"OPENROUTER", "TYPESAFE_DIRECT"}
            else "UNKNOWN"
        )
        safe_cost = None
        if usage_cost_usd is not None:
            try:
                candidate_cost = float(usage_cost_usd)
            except (TypeError, ValueError, OverflowError):
                candidate_cost = -1.0
            if math.isfinite(candidate_cost) and 0.0 <= candidate_cost <= 1_000_000.0:
                safe_cost = candidate_cost
        self.store.record_structural_for_key(
            key,
            {
                "kind": "jevRoutingTransport",
                "statusCode": None if status_code is None else int(status_code),
                "latencyMs": max(0.0, min(120_000.0, float(latency_ms))),
                "gateway": safe_gateway,
                "decisionModel": str(decision_model)[:128],
                "routerModel": None if router_model is None else str(router_model)[:128],
                "inputTokens": None if input_tokens is None else max(0, int(input_tokens)),
                "outputTokens": None if output_tokens is None else max(0, int(output_tokens)),
                "usageCostUsd": safe_cost,
                "fallback": bool(fallback),
            },
        )

    def record_media_failure(
        self,
        *,
        media_branch: str,
        failure_stage: str,
        error: BaseException,
    ) -> None:
        """Persist only bounded, value-free OCR keyframe failure metadata."""

        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if (
            key is None
            or media_branch != "OCR"
            or failure_stage != "KEYFRAME_EXTRACTION"
        ):
            return
        process_started: bool | None = None
        if isinstance(error, SubprocessLaunchError):
            process_started = False
        elif isinstance(
            error,
            (SubprocessExecutionError, SubprocessTimeoutError),
        ):
            process_started = True
        raw_exit_code = getattr(error, "returncode", None)
        exit_code = (
            raw_exit_code
            if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool)
            else None
        )
        self.store.record_structural_for_key(
            key,
            {
                "kind": "mediaFailure",
                "mediaBranch": "OCR",
                "failureStage": "KEYFRAME_EXTRACTION",
                "errorClass": type(error).__name__[:80],
                "processStarted": process_started,
                "exitCode": exit_code,
            },
        )

    def record_executor_structural_attempt(
        self,
        *,
        attempt: int,
        repair_triggered: bool,
        repair_succeeded: bool,
    ) -> None:
        """Persist bounded structural-repair state in the existing trace."""

        if self._scoped_counters.get() is not None:
            return
        key = self._current_key.get()
        if key is None:
            return
        self.store.record_structural_for_key(
            key,
            {
                "kind": "executorStructuralAttempt",
                "stage": "EXECUTOR",
                "model": self._model_identifier,
                "executorStructuralAttempt": max(1, min(2, int(attempt))),
                "executorStructuralRepairTriggered": bool(repair_triggered),
                "executorStructuralRepairSucceeded": bool(repair_succeeded),
            },
        )


def _role_name(stage: str) -> str:
    normalized = str(stage).upper()
    if normalized in {"PLANNER", "PLANNER_REPAIR", "REPLANNER"}:
        return "PLANNER"
    if normalized in {"EXECUTOR", "EXECUTOR_REPAIR"}:
        return "EXECUTOR"
    if normalized == "CRITIC":
        return "CRITIC"
    if normalized == "RETRIEVAL_PLANNER":
        return "RETRIEVAL_PLANNER"
    if normalized == "CHUNK_SUMMARY":
        return "CHUNK_SUMMARY"
    return normalized or "MODEL"
